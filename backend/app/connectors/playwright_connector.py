"""Optional isolated read-only Playwright connector for JS-rendered public job boards.

Strict boundaries:
- Verifies `robots.txt` via `SafeAsyncHttpClient` BEFORE opening headless browser.
- Read-only navigation: no cookies imported, no login, no form filling, no CAPTCHA solving.
- If anti-bot gate appears or Playwright browser binaries are absent, aborts cleanly and
  preserves a Tier 3 lead (`access_blocked=True`, `incomplete_summary=True`).
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.connectors.http_client import (
     CAPTCHA_SIGNATURES,
    AccessRestrictedError,
    ConnectorError,
    SafeAsyncHttpClient,
    html_to_clean_text,
)
from app.domain.triage_engine import RawJobInput
from app.models.criteria_schema import SearchCriteria
from app.models.sources_schema import PlaywrightPublicBoardConfig


class PlaywrightPublicConnector:
    def __init__(
        self,
        config: PlaywrightPublicBoardConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = f"playwright_public:{config.id}"

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        url = self.config.listing_url
        errors: list[dict[str, Any]] = []

        try:
            await self.client.ensure_robots_allowed(
                url, rate_limit_seconds=self.config.rate_limit_seconds
            )
        except AccessRestrictedError as exc:
            return [], [{"url": url, "error": str(exc)}], {"extracted_jobs": 0}

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return [], [{"url": url, "error": "Playwright package not installed"}], {"extracted_jobs": 0}

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        java_script_enabled=True,
                        accept_downloads=False,
                    )
                    page = await context.new_page()
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    status = response.status if response else 0
                    if status in {401, 403, 429}:
                        raise AccessRestrictedError(
                            f"HTTP {status} on Playwright public page", status_code=status
                        )
                    content = await page.content()
                    if CAPTCHA_SIGNATURES.search(content):
                        raise AccessRestrictedError("CAPTCHA / anti-bot challenge detected; aborting.")
                    title = (await page.title()).strip() or f"Vagas — {self.config.company_name}"
                    text = html_to_clean_text(content)
                finally:
                    await browser.close()
        except Exception as exc:
            errors.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            fallback = RawJobInput(
                source=self.source_key,
                source_id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
                title=f"Portal de Carreiras — {self.config.company_name}",
                company=self.config.company_name,
                url=url,
                description=f"Portal público {self.config.company_name}: verificar vagas manualmente.",
                incomplete_summary=True,
                access_blocked=True,
            )
            return [fallback], errors, {"extracted_jobs": 1, "fallback_lead": True}

        job = RawJobInput(
            source=self.source_key,
            source_id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
            title=title[:200],
            company=self.config.company_name,
            url=url,
            description=text[:80000],
            incomplete_summary=len(text) < 120,
        )
        return [job], errors, {"extracted_jobs": 1}
