"""Resilient `WebSearchJobConnector` (`web_search`) for heavily gated portals & SPA fallback.

Captures recent (`<= 14d`) individual job postings in Recife/RMR and Remote Brazil from
portals protected by Cloudflare/anti-bot gates or client-rendered SPAs (Indeed Brasil,
Glassdoor, Jooble, Sólides, Trabalha Brasil, Coodesh, Porto Digital, Catho, Vagas.com.br, InfoJobs):
1. Keyless Job Meta-Search (`br.talent.com/jobs?date=14` indexing Indeed, Jooble, Glassdoor,
   Catho, InfoJobs, Sólides, Randstad, and Recife/PE employers with explicit relative dates).
2. Organic `site:` search parsing (DuckDuckGo HTML / Bing RSS) when reachable without challenge,
   strictly filtered by `is_verified_individual_job_url` + `is_garbage_job_candidate`.
3. Optional Google Search Grounding via Gemini (`tools=[{"google_search": {}}]`) when a Google
   API key is configured (using exclusively generic role/location terms — zero PII).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
import xml.etree.ElementTree as ET
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit

import httpx
import truststore
from selectolax.lexbor import LexborHTMLParser

from app.connectors.anti_garbage import (
    is_garbage_job_candidate,
    is_verified_individual_job_url,
)
from app.connectors.http_client import (
    AccessRestrictedError,
    BudgetExhaustedError,
    ConnectorError,
    SafeAsyncHttpClient,
)
from app.domain.date_utils import (
    is_published_within_days,
    normalize_published_iso,
)
from app.domain.triage_engine import RawJobInput, contains_term, normalize_text
from app.models.criteria_schema import SearchCriteria
from app.models.sources_schema import WebSearchSourceConfig

MAX_RECENCY_DAYS = 14


def _decode_search_redirect_url(raw_href: str) -> str:
    href = (raw_href or "").strip()
    if not href:
        return ""
    if "duckduckgo.com/l/?" in href or href.startswith("//duckduckgo.com/l/?"):
        qs = parse_qs(urlsplit(href).query)
        if qs.get("uddg"):
            return unquote(qs["uddg"][0]).strip()
    m_bing = re.search(r"[?&]u=a1([A-Za-z0-9_-]+)", href)
    if m_bing:
        b64 = m_bing.group(1)
        pad = b64 + "=" * (-len(b64) % 4)
        try:
            return base64.urlsafe_b64decode(pad).decode("utf-8", errors="ignore").strip()
        except Exception:
            return href
    return href


def _extract_relative_age(text: str) -> str | None:
    if not text:
        return None
    m = re.search(
        r"(?:[úu]ltima\s+atualiza[cç][aã]o\s*:\s*|publicad[ao]\s+)?"
        r"(?:hoje|ontem|anteontem|agora\s+mesmo|"
        r"(?:h[aá]\s+)?(?:\d+|um|uma)\+?\s*(?:minuto|minutos|min|hora|horas|dia|dias|semana|semanas|m[eê]s|meses)"
        r"(?:\s+atr[aá]s)?)",
        text,
        re.IGNORECASE,
    )
    return m.group(0).strip() if m else None


def _is_senior_role(title: str, criteria: SearchCriteria) -> bool:
    return any(
        contains_term(title, sig)
        for sig in criteria.career_focus.out_of_focus_seniority_signals
    )


def _matches_any_catalog_area(title: str, snippet: str, criteria: SearchCriteria) -> bool:
    blob = f"{title} {snippet}"
    for terms in criteria.role_catalog.area_keywords.values():
        if any(contains_term(blob, t) for t in terms):
            return True
    return False


def _location_matches_criteria(
    location: str | None,
    snippet: str,
    query_loc: str,
    criteria: SearchCriteria,
) -> tuple[bool, str | None]:
    norm_loc = normalize_text(f"{location or ''} {snippet[:220]}")
    if criteria.geography.accept_remote_national and any(
        w in norm_loc for w in ("remoto", "home office", "teletrabalho", "remote", "qualquer lugar")
    ):
        return True, "remoto"

    city_tokens: set[str] = set()
    state_tokens: set[str] = set()
    for city in criteria.geography.allowed_cities:
        city_tokens.add(normalize_text(city.canonical_name))
        for alias in city.aliases:
            city_tokens.add(normalize_text(alias))
        st = normalize_text(city.state_code)
        if st:
            state_tokens.add(f"- {st}")
            state_tokens.add(f"/{st}")
            state_tokens.add(f", {st}")
            if st == "pe":
                city_tokens.update({"pernambuco", "olinda", "paulista", "camaragibe"})
            elif st == "mg":
                city_tokens.add("minas gerais")

    if any(tok and tok in norm_loc for tok in city_tokens) or any(
        st in norm_loc for st in state_tokens
    ):
        wp = "hibrido" if "hibrid" in norm_loc else "presencial"
        return True, wp
    if criteria.geography.accept_remote_national and "remoto" in normalize_text(query_loc):
        return True, "remoto"
    return False, None


class WebSearchJobConnector:
    """Discovers fresh individual job listings via keyless meta-search, `site:` search, and optional Gemini Grounding."""

    def __init__(
        self,
        config: WebSearchSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
        *,
        gemini_api_key: str = "",
        llm_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.gemini_api_key = gemini_api_key.strip()
        self.llm_transport = llm_transport
        self.source_key = f"web_search:{config.id}"

    def _default_queries(self) -> list[str]:
        if self.config.queries:
            return self.config.queries
        if self.config.company_fallback:
            return [
                f"{self.config.company_fallback} TI suporte desenvolvedor dados",
                f"{self.config.company_fallback} tecnologia sistemas",
            ]
        return [
            "suporte ti help desk redes",
            "desenvolvedor junior sistemas dados",
            "analista de dados junior",
        ]

    def _parse_talent_meta_cards(
        self,
        raw_html: str,
        query_loc: str,
    ) -> list[dict[str, Any]]:
        tree = LexborHTMLParser(raw_html)
        cards: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for a_node in tree.css("a[href*='/view?id=']"):
            href = (a_node.attributes.get("href") or "").strip()
            m_id = re.search(r"id=(\d+)", href)
            if not m_id:
                continue
            job_id = m_id.group(1)
            if job_id in seen_ids:
                continue

            # Walk up to card container holding h2 + company + age + snippet
            container = a_node
            for _ in range(6):
                if container.parent is None:
                    break
                container = container.parent
                if container.css_first("h2"):
                    break

            h2 = container.css_first("h2")
            title = (h2.text(strip=True) if h2 else a_node.text(strip=True)).strip()
            full_url = urljoin("https://br.talent.com", f"/view?id={job_id}")
            if is_garbage_job_candidate(title, full_url):
                continue

            parts = [
                p.strip()
                for p in container.text(separator="|", strip=True).split("|")
                if p.strip() and p.strip() not in {"•", "Mostre mais", "Candidatura rápida"}
            ]
            age_raw: str | None = None
            company = self.config.company_fallback
            location: str | None = None
            snippet_chunks: list[str] = []

            for idx, part in enumerate(parts):
                if not age_raw:
                    extracted_age = _extract_relative_age(part)
                    if extracted_age:
                        age_raw = extracted_age
                        continue
                if part == title:
                    # Usually right after `title` comes `Company` and `City, State, BR`
                    if idx + 1 < len(parts) and len(parts[idx + 1]) <= 90:
                        cand_co = parts[idx + 1]
                        if not _extract_relative_age(cand_co) and ", BR" not in cand_co:
                            company = cand_co
                    if idx + 2 < len(parts) and len(parts[idx + 2]) <= 90:
                        cand_loc = parts[idx + 2]
                        if any(tok in cand_loc for tok in (", BR", "Pernambuco", "Recife", "Jaboatão", "Remoto", "Home Office", " - ")):
                            location = cand_loc.replace(", BR", "").strip()
                    continue
                if len(part) > 45:
                    snippet_chunks.append(part)

            seen_ids.add(job_id)
            cards.append(
                {
                    "id": f"talent_{job_id}",
                    "title": title,
                    "company": company,
                    "location": location or query_loc,
                    "url": full_url,
                    "published_at": age_raw,
                    "snippet": " ".join(snippet_chunks)[:2500] or title,
                }
            )
        return cards

    def _parse_organic_serp_cards(self, raw_text: str, is_rss: bool = False) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        if is_rss and raw_text.lstrip().startswith("<"):
            try:
                root = ET.fromstring(raw_text)
                for item in root.findall("./channel/item"):
                    raw_title = unescape(item.findtext("title", "")).strip()
                    raw_link = _decode_search_redirect_url(item.findtext("link", ""))
                    desc = unescape(item.findtext("description", "")).strip()
                    pub = item.findtext("pubDate")
                    if not raw_link.startswith("https://") or raw_link in seen_urls:
                        continue
                    clean_title = re.sub(
                        r"\s*[-|–]\s*(?:Indeed(?:\.com)?|Catho|Vagas\.com(?:\.br)?|InfoJobs|Glassdoor|Jooble|LinkedIn).*$",
                        "",
                        raw_title,
                        flags=re.IGNORECASE,
                    ).strip()
                    if is_garbage_job_candidate(clean_title, raw_link) or not is_verified_individual_job_url(raw_link):
                        continue
                    seen_urls.add(raw_link)
                    results.append(
                        {
                            "id": hashlib.sha256(raw_link.encode("utf-8")).hexdigest()[:24],
                            "title": clean_title,
                            "company": self.config.company_fallback,
                            "location": None,
                            "url": raw_link,
                            "published_at": pub or _extract_relative_age(desc),
                            "snippet": desc or clean_title,
                        }
                    )
            except ET.ParseError:
                return []
            return results

        tree = LexborHTMLParser(raw_text)
        for node in tree.css("div.result, li.b_algo"):
            a_el = node.css_first("a.result__a, h2 a")
            if a_el is None:
                continue
            raw_title = a_el.text(strip=True)
            raw_link = _decode_search_redirect_url(a_el.attributes.get("href") or "")
            if not raw_link.startswith("https://") or raw_link in seen_urls:
                continue
            clean_title = re.sub(
                r"\s*[-|–]\s*(?:Indeed(?:\.com)?|Catho|Vagas\.com(?:\.br)?|InfoJobs|Glassdoor|Jooble|LinkedIn).*$",
                "",
                raw_title,
                flags=re.IGNORECASE,
            ).strip()
            if is_garbage_job_candidate(clean_title, raw_link) or not is_verified_individual_job_url(raw_link):
                continue
            snip_el = node.css_first(".result__snippet, .b_caption p")
            snippet = snip_el.text(strip=True) if snip_el else clean_title
            seen_urls.add(raw_link)
            results.append(
                {
                    "id": hashlib.sha256(raw_link.encode("utf-8")).hexdigest()[:24],
                    "title": clean_title,
                    "company": self.config.company_fallback,
                    "location": None,
                    "url": raw_link,
                    "published_at": _extract_relative_age(snippet),
                    "snippet": snippet,
                }
            )
        return results

    async def _query_gemini_search_grounding(self, queries: list[str]) -> list[dict[str, Any]]:
        """Optional Google Search Grounding via Gemini (`tools=[{"google_search": {}}]`). Zero PII sent."""
        if not self.gemini_api_key:
            return []

        domains_expr = " OR ".join(
            f"site:{d}" for d in (self.config.target_domains or ["br.indeed.com", "glassdoor.com.br", "vagas.com.br", "catho.com.br"])[:5]
        )
        roles_expr = "; ".join(queries[:3])
        locs_expr = ", ".join(self.config.locations[:3])
        prompt = (
            f"Encontre vagas de emprego reais e recentes (últimos 14 dias) publicadas em ({domains_expr}) "
            f"para os perfis [{roles_expr}] nas localidades [{locs_expr}] (nível júnior, assistente, técnico ou estágio remunerado). "
            "Retorne APENAS um array JSON puro onde cada item tenha: "
            '{"title": "...", "company": "...", "location": "...", "url": "https://...", "published_at": "...", "snippet": "..."}.'
        )
        endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
        }
        try:
            if self.llm_transport is not None:
                async with httpx.AsyncClient(transport=self.llm_transport, timeout=8.0) as c:
                    resp = await c.post(endpoint, headers={"x-goog-api-key": self.gemini_api_key}, json=body)
            else:
                ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                async with httpx.AsyncClient(verify=ssl_ctx, timeout=8.0) as c:
                    resp = await c.post(endpoint, headers={"x-goog-api-key": self.gemini_api_key}, json=body)
            if resp.status_code != 200:
                return []
            data = resp.json()
            cand = (data.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            raw_txt = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
            m_arr = re.search(r"\[[\s\S]*\]", raw_txt)
            if not m_arr:
                return []
            parsed = json.loads(m_arr.group(0))
            if not isinstance(parsed, list):
                return []
            out: list[dict[str, Any]] = []
            for row in parsed:
                if not isinstance(row, dict):
                    continue
                u = str(row.get("url") or "").strip()
                t = str(row.get("title") or "").strip()
                if not u.startswith("https://") or is_garbage_job_candidate(t, u) or not is_verified_individual_job_url(u):
                    continue
                out.append(
                    {
                        "id": hashlib.sha256(u.encode("utf-8")).hexdigest()[:24],
                        "title": t,
                        "company": row.get("company") or self.config.company_fallback,
                        "location": row.get("location"),
                        "url": u,
                        "published_at": row.get("published_at"),
                        "snippet": str(row.get("snippet") or t),
                    }
                )
            return out
        except Exception:
            return []

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = seen_source_ids or {}
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []
        raw_candidates: list[dict[str, Any]] = []

        queries = self._default_queries()
        locations = self.config.locations or ["Recife, PE", "Remoto"]

        # Layer 1: Keyless Brazilian Job Meta-Search (`br.talent.com/jobs?date=14`)
        for q in queries[:4]:
            for loc in locations[:2]:
                if len(raw_candidates) >= self.config.max_items * 3:
                    break
                loc_param = "Brasil" if "remoto" in normalize_text(loc) else loc
                kw_param = f"{q} remoto" if "remoto" in normalize_text(loc) else q
                meta_url = (
                    f"https://br.talent.com/jobs?k={quote_plus(kw_param)}"
                    f"&l={quote_plus(loc_param)}&date=14"
                )
                try:
                    html = await self.client.get_text(
                        meta_url,
                        check_robots=False,
                        rate_limit_seconds=self.config.rate_limit_seconds,
                    )
                    raw_candidates.extend(self._parse_talent_meta_cards(html, loc))
                except BudgetExhaustedError:
                    break
                except ConnectorError as exc:
                    errors.append({"url": meta_url, "error": str(exc)})

        # Layer 2: Organic `site:` search (DuckDuckGo HTML) when target_domains are specified and candidates are scarce
        if self.config.target_domains and len(raw_candidates) < self.config.max_items:
            for dom in self.config.target_domains[:2]:
                dork = f"site:{dom} {queries[0]} Recife OR Remoto"
                ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(dork)}&df=w"
                try:
                    ddg_html = await self.client.get_text(
                        ddg_url,
                        check_robots=False,
                        rate_limit_seconds=self.config.rate_limit_seconds,
                    )
                    raw_candidates.extend(self._parse_organic_serp_cards(ddg_html, is_rss=False))
                except BudgetExhaustedError:
                    break
                except (AccessRestrictedError, ConnectorError):
                    # Respect 202/403/429 immediately without retry or bypass
                    break

        # Layer 3: Optional Google Search Grounding via Gemini when configured
        if self.gemini_api_key and len(raw_candidates) < self.config.max_items:
            grounded = await self._query_gemini_search_grounding(queries)
            raw_candidates.extend(grounded)

        seen_batch: set[str] = set()
        for item in raw_candidates:
            if len(jobs) >= self.config.max_items:
                break
            sid = str(item.get("id") or "")
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not sid or sid in seen or sid in seen_batch:
                continue
            if is_garbage_job_candidate(title, url):
                continue
            if _is_senior_role(title, self.criteria):
                continue

            snippet = str(item.get("snippet") or "")
            if not _matches_any_catalog_area(title, snippet, self.criteria):
                continue

            pub_raw = item.get("published_at")
            # Reject undated search snippets or items > 14 days old
            if not is_published_within_days(pub_raw, max_days=MAX_RECENCY_DAYS, allow_missing=False):
                continue

            loc_ok, inferred_wp = _location_matches_criteria(
                item.get("location"), snippet, str(item.get("location") or ""), self.criteria
            )
            if not loc_ok:
                continue

            if self.config.company_fallback:
                comp_norm = normalize_text(self.config.company_fallback)
                item_comp_norm = normalize_text(str(item.get("company") or ""))
                if comp_norm not in item_comp_norm and comp_norm not in normalize_text(f"{title} {snippet}"):
                    continue

            seen_batch.add(sid)
            company = str(item.get("company") or self.config.company_fallback or "").strip() or None
            location = str(item.get("location") or "").strip() or ("Remoto" if inferred_wp == "remoto" else "Recife, PE")
            opp_type = (
                "banco_talentos"
                if re.search(r"banco\s+de\s+talentos|talent\s+pool", normalize_text(title))
                else "vaga"
            )
            desc = (
                f"{title}\n"
                f"Empresa: {company or 'Não informada'}\n"
                f"Localidade: {location}\n\n"
                f"{snippet}"
            ).strip()

            jobs.append(
                RawJobInput(
                    source=self.source_key,
                    source_id=sid,
                    title=title,
                    company=company,
                    url=url,
                    description=desc[:20000],
                    location=location,
                    workplace_mode=inferred_wp,  # type: ignore[arg-type]
                    published_at=normalize_published_iso(pub_raw),
                    opportunity_type=opp_type,  # type: ignore[arg-type]
                    incomplete_summary=len(snippet) < 140,
                    requires_login=self.config.requires_login_for_apply,
                    validity_status="aparentemente_aberta",
                )
            )

        stats = {
            "search_candidates": len(raw_candidates),
            "extracted_jobs": len(jobs),
        }
        return jobs, errors, stats
