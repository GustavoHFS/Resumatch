"""Safe async HTTP client and Lexbor HTML/JSON-LD parser (`httpx` + `truststore` + `selectolax`).

Security & politeness guarantees:
- Strict `https://` and host allowlist enforcement (no credentials, no custom ports).
- In-memory `robots.txt` cache per host with `Crawl-delay` support.
- Per-domain async rate-limiting and hard per-run request budget (`max_requests_budget`).
- Immediate clean abort (`AccessRestrictedError`) on HTTP 401/403/429, unexpected redirects,
  or anti-bot/CAPTCHA challenge signatures — never bypassing controls.
- Read-only JSON-RPC POST strictly restricted to the public `buscar_vagas` MCP tool.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import truststore
from selectolax.lexbor import LexborHTMLParser

USER_AGENT = "ResumatchLocal/0.1 (public read-only job discovery)"

CAPTCHA_SIGNATURES = re.compile(
    r"cf-chl-|cf-turnstile|verify you are human|captcha challenge|g-recaptcha|hcaptcha",
    re.IGNORECASE,
)

SENSITIVE_QUERY_KEYS = re.compile(
    r"token|password|senha|secret|authorization|credential|api[_-]?key",
    re.IGNORECASE,
)

BLOCK_TAGS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "ul", "ol", "section", "article", "tr"}


class ConnectorError(RuntimeError):
    """Base error for connector failures."""


class AccessRestrictedError(ConnectorError):
    """Raised on HTTP 401/403/429, redirects to auth, robots disallow, or CAPTCHA gate."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BudgetExhaustedError(ConnectorError):
    """Raised when a discovery run reaches its configured request budget."""


def validate_public_https_url(url: str, allowed_hosts: set[str] | None = None) -> tuple[str, str]:
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or not parsed.hostname:
        raise ConnectorError(f"Invalid or non-HTTPS public URL rejected: {url}")
    host = parsed.hostname.lower()
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ConnectorError(f"Host '{host}' is not in the configured allowlist.")
    for key, _ in parse_qsl(parsed.query):
        if SENSITIVE_QUERY_KEYS.search(key):
            raise ConnectorError("URL containing authentication query parameter rejected.")
    return url.strip(), host


def html_to_clean_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    tree = LexborHTMLParser(unescape(raw_html))
    if tree.body is None:
        return unescape(raw_html).strip()

    for tag in ("script", "style", "noscript", "form", "svg", "iframe"):
        for node in tree.css(tag):
            node.decompose()

    # Insert newlines around block elements so Lexbor text extraction preserves paragraphs/lists
    for tag in BLOCK_TAGS:
        for node in tree.css(tag):
            if tag == "br":
                node.replace_with("\n")
            else:
                node.insert_before("\n")
                node.insert_after("\n")

    raw_text = tree.body.text(separator=" ") if tree.body else ""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw_text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_json_ld_job_posting(raw_html: str) -> dict[str, Any] | None:
    if not raw_html:
        return None
    tree = LexborHTMLParser(raw_html)
    for script_node in tree.css('script[type="application/ld+json"]'):
        content = script_node.text(deep=True).strip()
        if not content:
            continue
        try:
            payload = json.loads(content)
        except ValueError:
            continue
        candidates = (
            payload
            if isinstance(payload, list)
            else payload.get("@graph", [payload])
            if isinstance(payload, dict)
            else []
        )
        for item in candidates:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            type_list = types if isinstance(types, list) else [types]
            if any(isinstance(t, str) and "JobPosting" in t for t in type_list):
                return item
    return None


class SafeAsyncHttpClient:
    """Polite, read-only async HTTP client with OS truststore, robots.txt cache, and budget limits."""

    def __init__(
        self,
        allowed_hosts: set[str],
        max_requests_budget: int = 60,
        default_rate_limit_seconds: float = 0.35,
        max_response_bytes: int = 8_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.allowed_hosts = {h.lower() for h in allowed_hosts} | {
            "boards-api.greenhouse.io",
            "api.lever.co",
            "www.empregare.com",
        }
        self.max_requests_budget = max_requests_budget
        self.default_rate_limit_seconds = default_rate_limit_seconds
        self.max_response_bytes = max_response_bytes
        self.requests_made = 0
        self._robots_cache: dict[str, RobotFileParser] = {}
        self._domain_delays: dict[str, float] = {}
        self._last_request_at: dict[str, float] = {}
        self._response_cache: dict[tuple[str, str], Any] = {}

        self._is_mock_transport = transport is not None
        if transport is not None:
            self._client = httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=httpx.Timeout(20.0),
                headers={"User-Agent": USER_AGENT},
            )
        else:
            ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self._client = httpx.AsyncClient(
                verify=ssl_ctx,
                follow_redirects=False,
                timeout=httpx.Timeout(20.0),
                headers={"User-Agent": USER_AGENT},
            )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SafeAsyncHttpClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()

    async def _throttle_domain(self, host: str, min_delay: float | None = None) -> None:
        if self._is_mock_transport and self.default_rate_limit_seconds == 0.0:
            return
        delay = max(
            min_delay if min_delay is not None else self.default_rate_limit_seconds,
            self._domain_delays.get(host, 0.0),
        )
        if delay <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_request_at.get(host, 0.0)
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)

    def _consume_budget(self) -> None:
        if self.requests_made >= self.max_requests_budget:
            raise BudgetExhaustedError(
                f"Discovery run request budget exhausted ({self.max_requests_budget} requests)."
            )
        self.requests_made += 1

    async def ensure_robots_allowed(self, url: str, rate_limit_seconds: float | None = None) -> None:
        _, host = validate_public_https_url(url, self.allowed_hosts)
        if host not in self._robots_cache:
            robots_url = (
                "https://www.empregare.com/pt-br/robots.txt"
                if host == "www.empregare.com"
                else f"https://{host}/robots.txt"
            )
            parser = RobotFileParser(robots_url)
            try:
                robots_txt = await self.get_text(
                    robots_url,
                    check_robots=False,
                    rate_limit_seconds=rate_limit_seconds,
                    allow_404_empty=True,
                )
                parser.parse(robots_txt.splitlines())
            except AccessRestrictedError:
                # If robots.txt itself returns 403/429, treat entire host as restricted
                raise
            self._robots_cache[host] = parser
            crawl_delay = parser.crawl_delay("ResumatchLocal") or parser.crawl_delay("*")
            if crawl_delay:
                self._domain_delays[host] = float(crawl_delay)

        policy = self._robots_cache[host]
        if not policy.can_fetch("ResumatchLocal", url):
            raise AccessRestrictedError(f"Disallowed by robots.txt policy on {host}")

    async def get_text(
        self,
        url: str,
        *,
        check_robots: bool = True,
        rate_limit_seconds: float | None = None,
        allow_404_empty: bool = False,
    ) -> str:
        clean_url, host = validate_public_https_url(url, self.allowed_hosts)
        cache_key = ("TEXT", clean_url)
        if cache_key in self._response_cache:
            return self._response_cache[cache_key]

        if check_robots:
            await self.ensure_robots_allowed(clean_url, rate_limit_seconds=rate_limit_seconds)

        self._consume_budget()
        await self._throttle_domain(host, rate_limit_seconds)
        try:
            response = await self._client.get(
                clean_url,
                headers={"Accept": "text/html,application/rss+xml,application/xml,text/plain;q=0.9"},
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Network error fetching {host}: {type(exc).__name__}") from exc
        finally:
            self._last_request_at[host] = time.monotonic()

        status = response.status_code
        if allow_404_empty and status == 404:
            self._response_cache[cache_key] = ""
            return ""
        if status in {401, 403, 429} or 300 <= status < 400:
            raise AccessRestrictedError(
                f"HTTP {status} restriction/redirect on {host}; aborting without bypass.",
                status_code=status,
            )
        if status != 200:
            raise ConnectorError(f"Unexpected HTTP {status} from {host}")

        if len(response.content) > self.max_response_bytes:
            raise ConnectorError(f"Response from {host} exceeded byte limit.")

        try:
            decoded = response.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = response.content.decode("cp1252", errors="replace")

        if CAPTCHA_SIGNATURES.search(decoded):
            raise AccessRestrictedError(
                f"Anti-bot / CAPTCHA gate detected on {host}; aborting immediately."
            )

        self._response_cache[cache_key] = decoded
        return decoded

    async def get_json(
        self,
        url: str,
        *,
        rate_limit_seconds: float | None = None,
    ) -> Any:
        clean_url, host = validate_public_https_url(url, self.allowed_hosts)
        cache_key = ("JSON", clean_url)
        if cache_key in self._response_cache:
            return self._response_cache[cache_key]

        self._consume_budget()
        await self._throttle_domain(host, rate_limit_seconds)
        try:
            response = await self._client.get(
                clean_url,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Network error fetching JSON from {host}: {type(exc).__name__}") from exc
        finally:
            self._last_request_at[host] = time.monotonic()

        status = response.status_code
        if status in {401, 403, 429} or 300 <= status < 400:
            raise AccessRestrictedError(
                f"HTTP {status} on JSON endpoint {host}",
                status_code=status,
            )
        if status in {404, 410}:
            raise AccessRestrictedError(f"Resource closed/missing ({status})", status_code=status)
        if status != 200:
            raise ConnectorError(f"Unexpected HTTP {status} from {host}")

        if "json" not in response.headers.get("content-type", "").lower():
            raise AccessRestrictedError(f"Non-JSON response from {host}; possible gateway block.")

        data = response.json()
        self._response_cache[cache_key] = data
        return data

    async def post_empregare_mcp_readonly(
        self,
        endpoint: str,
        method: str,
        params: dict[str, Any] | None = None,
        rate_limit_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Strictly read-only JSON-RPC call limited to `initialize`, `tools/list`, and `buscar_vagas`."""
        clean_url, host = validate_public_https_url(endpoint, self.allowed_hosts)
        if host != "www.empregare.com" or not clean_url.endswith("/api/mcp"):
            raise ConnectorError("MCP JSON-RPC is restricted to the official Empregare read-only endpoint.")
        if method not in {"initialize", "tools/list", "tools/call"} or (
            method == "tools/call" and (params or {}).get("name") != "buscar_vagas"
        ):
            raise ConnectorError(f"Forbidden MCP method/tool '{method}': strictly read-only buscar_vagas allowed.")

        self._consume_budget()
        await self._throttle_domain(host, rate_limit_seconds)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            payload["params"] = params

        try:
            response = await self._client.post(
                clean_url,
                json=payload,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-03-26",
                },
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"MCP network failure: {type(exc).__name__}") from exc
        finally:
            self._last_request_at[host] = time.monotonic()

        if response.status_code in {401, 403, 429} or 300 <= response.status_code < 400:
            raise AccessRestrictedError(
                f"HTTP {response.status_code} on Empregare MCP",
                status_code=response.status_code,
            )
        if response.status_code != 200:
            raise ConnectorError(f"Empregare MCP returned HTTP {response.status_code}")

        text = response.text.strip()
        if text.startswith("{"):
            parsed = json.loads(text)
        else:
            sse_msgs = [
                json.loads(line[5:].strip())
                for line in text.splitlines()
                if line.startswith("data:")
            ]
            parsed = next((m for m in sse_msgs if "result" in m or "error" in m), None)

        if not isinstance(parsed, dict) or "error" in parsed:
            raise ConnectorError("Invalid or error response from Empregare MCP")
        return parsed["result"]
