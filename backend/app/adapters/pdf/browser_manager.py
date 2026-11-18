"""Playwright Headless Chromium lifecycle manager with lazy reuse and idle-timeout auto-close.

Guarantees:
- Zero cold-start on FastAPI boot (launches Chromium lazily on first PDF render).
- Reuses warm browser across consecutive PDF exports.
- Strictly blocks all external network requests (`http://`, `https://`, `ws://`, `file://`)
  during HTML-to-PDF rendering (`data:` / `about:` only).
- Automatically closes Chromium after `idle_timeout_seconds` of inactivity and cleans up
  deterministically during FastAPI `lifespan` shutdown.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class PdfRenderError(RuntimeError):
    """Raised when Chromium PDF generation fails or violates offline constraints."""


class ChromiumPdfBrowserManager:
    def __init__(self, idle_timeout_seconds: float = 120.0) -> None:
        self.idle_timeout_seconds = max(0.1, float(idle_timeout_seconds))
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._active_renders: int = 0
        self._last_used_at: float = 0.0
        self._watchdog_task: asyncio.Task[None] | None = None

        # Observability counters
        self.launch_count: int = 0
        self.render_count: int = 0
        self.idle_close_count: int = 0

    @property
    def is_browser_running(self) -> bool:
        return self._browser is not None and bool(self._browser.is_connected())

    async def _ensure_browser_unlocked(self) -> Any:
        if self._browser is not None and self._browser.is_connected():
            return self._browser

        await self._close_resources_unlocked()
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--disable-gpu", "--no-first-run"],
        )
        self.launch_count += 1
        return self._browser

    def _schedule_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = asyncio.create_task(self._idle_watchdog())

    async def _idle_watchdog(self) -> None:
        try:
            while True:
                elapsed = time.monotonic() - self._last_used_at
                remaining = self.idle_timeout_seconds - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                async with self._lock:
                    if (
                        self._active_renders == 0
                        and self._browser is not None
                        and (time.monotonic() - self._last_used_at) >= self.idle_timeout_seconds
                    ):
                        await self._close_resources_unlocked()
                        self.idle_close_count += 1
                        return
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return

    async def _close_resources_unlocked(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def render_html_to_pdf(self, html_content: str, timeout_ms: int = 10_000) -> bytes:
        async with self._lock:
            browser = await self._ensure_browser_unlocked()
            self._active_renders += 1
            self._last_used_at = time.monotonic()

        context = None
        try:
            context = await browser.new_context(
                java_script_enabled=False,
                offline=False,
            )

            async def _offline_guard(route: Any) -> None:
                url = str(route.request.url)
                if url.startswith(("about:", "data:")):
                    await route.continue_()
                else:
                    await route.abort("blockedbyclient")

            await context.route("**/*", _offline_guard)
            page = await context.new_page()
            page.set_default_timeout(timeout_ms)
            await page.set_content(html_content, wait_until="load", timeout=timeout_ms)
            pdf_bytes: bytes = await page.pdf(
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
            )
            self.render_count += 1
            return pdf_bytes
        except Exception as exc:
            raise PdfRenderError(f"Headless Chromium PDF render failed: {exc}") from exc
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            async with self._lock:
                self._active_renders = max(0, self._active_renders - 1)
                self._last_used_at = time.monotonic()
                if self._browser is not None:
                    self._schedule_watchdog()

    async def aclose(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        async with self._lock:
            await self._close_resources_unlocked()
