"""Concrete deterministic job discovery connectors for configured public sources (v2.2).

Supports fast async `httpx` + `selectolax`/JSON connectors with strict 14-day recency,
safe redirect following, anti-garbage filtering, and web-search fallback:
- `RssAndPagesConnector` (`rss_local`, `public_pages`)
- `SitemapJsonLdConnector` (`sitemap_jsonld`)
- `GreenhouseAndLeverConnector` (`greenhouse`, `lever`)
- `AshbyAndRecruiteeConnector` (`ashby`, `recruitee`)
- `GupyApiConnector` (`gupy_api` — SSR `__NEXT_DATA__` on `portal.gupy.io` & company subdomains)
- `GitHubIssuesConnector` (`github_issues` — BR community repositories, ignoring pinned old issues)
- `InhireApiConnector` (`inhire` — `GET /job-posts/public/pages` with `X-Tenant` header)
- `WorkableApiConnector` (`workable` — public Workable widget API)
- `HtmlJobBoardConnector` (`html_board` — LinkedIn `jobs-guest` `.base-search-card` + detail API,
  Remotar JSON API, Trampos JSON API, Jobvite/Liferay, Vagas.com.br, Catho, InfoJobs, BNE,
  Programathor, GeekHunter, DivulgaVagas, with strict `is_garbage_job_candidate` enforcement)
- `WebSearchJobConnector` (`web_search` — keyless meta-search + `site:` + Gemini Grounding)
- `EmpregareMcpConnector` (`empregare_mcp` — PE + Remote filtered)
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Protocol
from urllib.parse import quote, quote_plus, urljoin, urlsplit

import httpx
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
    extract_json_ld_job_posting,
    html_to_clean_text,
)
from app.connectors.web_search import WebSearchJobConnector
from app.domain.date_utils import (
    is_published_within_days,
    normalize_published_iso,
    parse_published_at_utc,
)
from app.domain.triage_engine import RawJobInput, contains_term, normalize_text
from app.models.criteria_schema import SearchCriteria
from app.models.sources_schema import (
    AshbySourceConfig,
    EmpregareMcpSourceConfig,
    GitHubIssuesSourceConfig,
    GreenhouseSourceConfig,
    GupyApiSourceConfig,
    HtmlBoardSourceConfig,
    InhireSourceConfig,
    JobSourceEntry,
    LeverSourceConfig,
    PlaywrightPublicBoardConfig,
    PublicPagesSourceConfig,
    RecruiteeSourceConfig,
    RssLocalSourceConfig,
    SitemapSourceConfig,
    WebSearchSourceConfig,
    WorkableSourceConfig,
)

MAX_RECENCY_DAYS = 14

JOB_LINK_HINT_RE = re.compile(
    r"/(?:vaga|vagas|job|jobs|oportunidade|oportunidades|position|opening|vaga-de-)s?(?:/|-|_|\?|$)",
    re.IGNORECASE,
)

GITHUB_META_ISSUE_RE = re.compile(
    r"\b(?:regras|como\s+publicar|leia\s+antes|template\s+de\s+vaga|guia\s+de|aviso\s+importante|codigo\s+de\s+conduta)\b",
    re.IGNORECASE,
)


class JobConnectorProtocol(Protocol):
    source_key: str

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        ...


def collect_allowed_hosts(sources: list[JobSourceEntry]) -> set[str]:
    hosts: set[str] = {
        "portal.gupy.io",
        "portal.api.gupy.io",
        "api.github.com",
        "github.com",
        "api.inhire.app",
        "apply.workable.com",
        "api.ashbyhq.com",
        "api.remotar.com.br",
        "remotar.com.br",
        "trampos.co",
        "jobs.jobvite.com",
        "br.talent.com",
        "html.duckduckgo.com",
        "www.bing.com",
    }
    for src in sources:
        if isinstance(src, RssLocalSourceConfig):
            for u in src.feeds:
                if h := urlsplit(u).hostname:
                    hosts.add(h.lower())
            for p in src.seed_pages:
                if h := urlsplit(p.url).hostname:
                    hosts.add(h.lower())
        elif isinstance(src, PublicPagesSourceConfig):
            for p in src.pages:
                if h := urlsplit(p.url).hostname:
                    hosts.add(h.lower())
        elif isinstance(src, SitemapSourceConfig):
            for u in src.sitemaps:
                if h := urlsplit(u).hostname:
                    hosts.add(h.lower())
        elif isinstance(src, HtmlBoardSourceConfig):
            for u in src.search_urls:
                if h := urlsplit(u).hostname:
                    hosts.add(h.lower())
                    hosts.add(h.lower().removeprefix("www."))
                    hosts.add("www." + h.lower().removeprefix("www."))
        elif isinstance(src, InhireSourceConfig):
            if src.career_url and (h := urlsplit(src.career_url).hostname):
                hosts.add(h.lower())
            hosts.add(f"{src.tenant_slug.lower()}.inhire.app")
        elif isinstance(src, GupyApiSourceConfig):
            if src.career_subdomain:
                hosts.add(f"{src.career_subdomain.lower()}.gupy.io")
            if src.career_page_name:
                slug = re.sub(r"[^a-z0-9]", "", src.career_page_name.lower())
                if slug:
                    hosts.add(f"{slug}.gupy.io")
        elif isinstance(src, RecruiteeSourceConfig):
            hosts.add(f"{src.company_slug.lower()}.recruitee.com")
        elif isinstance(src, WebSearchSourceConfig):
            for dom in src.target_domains:
                clean_d = dom.lower().strip().removeprefix("https://").split("/")[0]
                if clean_d:
                    hosts.add(clean_d)
                    hosts.add(clean_d.removeprefix("www."))
                    hosts.add("www." + clean_d.removeprefix("www."))
        elif isinstance(src, EmpregareMcpSourceConfig):
            if h := urlsplit(src.endpoint).hostname:
                hosts.add(h.lower())
            for u in src.public_detail_urls_by_id.values():
                if h := urlsplit(u).hostname:
                    hosts.add(h.lower())
        elif isinstance(src, PlaywrightPublicBoardConfig):
            if h := urlsplit(src.listing_url).hostname:
                hosts.add(h.lower())
    return hosts


def _match_catalog_areas(title: str, criteria: SearchCriteria) -> list[str]:
    matched: list[str] = []
    for area, terms in criteria.role_catalog.area_keywords.items():
        if any(contains_term(title, t) for t in terms):
            matched.append(area)
    return matched


def _is_senior_title(title: str, criteria: SearchCriteria) -> bool:
    return any(
        contains_term(title, sig)
        for sig in criteria.career_focus.out_of_focus_seniority_signals
    )


def _infer_opportunity_type(title: str, internal_job_id: Any = 1) -> str:
    norm_t = normalize_text(title)
    if internal_job_id is None or re.search(r"banco\s+de\s+talentos|talent\s+pool", norm_t):
        return "banco_talentos"
    return "vaga"


def _extract_relative_date_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(
        r"\b(?:publicad[ao]\s+(?:em\s+)?|[úu]ltima\s+atualiza[cç][aã]o\s*:\s*)?"
        r"(?:hoje|ontem|anteontem|agora\s+mesmo|"
        r"h[aá]\s+(?:\d+|um|uma)\s*(?:minuto|minutos|min|hora|horas|dia|dias|semana|semanas|m[eê]s|meses)|"
        r"\d+\+?\s*(?:min|h|hora|horas|dia|dias|semana|semanas)\s+atr[aá]s|"
        r"\d{1,2}/\d{1,2}/20\d{2})\b",
        text,
        re.IGNORECASE,
    )
    return m.group(0) if m else None


def _extract_html_job_page(
    raw_html: str,
    url: str,
    source_key: str,
    seed: dict[str, Any] | None = None,
) -> RawJobInput:
    seed = seed or {}
    ld = extract_json_ld_job_posting(raw_html)
    tree = LexborHTMLParser(raw_html)

    h1_nodes = tree.css("h1")
    h1_title = h1_nodes[-1].text(strip=True) if h1_nodes else None
    title = (
        (html_to_clean_text(str(ld.get("title") or "")) if ld else None)
        or h1_title
        or seed.get("title")
    )

    desc_node = (
        tree.css_first(".job-description, .show-more-less-html__markup, .description__text")
        or tree.css_first(".section_content:not(.section_content_comments)")
        or tree.css_first(".general-content, .jv-job-detail-description")
        or tree.css_first("article")
        or tree.css_first("main")
    )
    if ld and ld.get("description"):
        description = html_to_clean_text(str(ld["description"]))
    elif desc_node is not None:
        description = html_to_clean_text(desc_node.html or "")
    else:
        description = html_to_clean_text(raw_html)

    description = description.split("Mais Oportunidades:", 1)[0].strip()
    if not title or len(description) < 20 or is_garbage_job_candidate(title, url):
        raise ConnectorError("Public HTML page lacks recognizable title or job description.")

    company = seed.get("company")
    if not company and ld and isinstance(ld.get("hiringOrganization"), dict):
        company = html_to_clean_text(str(ld["hiringOrganization"].get("name") or "")) or None
    if not company:
        comp_node = tree.css_first(".job-company-name, .topcard__org-name-link, .company")
        if comp_node:
            cand = comp_node.text(strip=True)
            if normalize_text(cand) not in {"admin", "agmaxx", ""}:
                company = cand
    if not company:
        m = re.search(r"(?im)^empresa\s*:\s*([^\n]+)", description)
        if m:
            company = m.group(1).strip()

    location = seed.get("location")
    if not location and ld and isinstance(ld.get("jobLocation"), (dict, list)):
        jloc = (
            ld["jobLocation"][0]
            if isinstance(ld["jobLocation"], list) and ld["jobLocation"]
            else ld["jobLocation"]
        )
        if isinstance(jloc, dict) and isinstance(jloc.get("address"), dict):
            addr = jloc["address"]
            city = addr.get("addressLocality")
            region = addr.get("addressRegion")
            location = " - ".join(x for x in [city, region] if x) or None
    if not location:
        loc_node = tree.css_first(".job-location, .topcard__flavor--bullet, .jv-job-detail-meta")
        if loc_node:
            cand_loc = loc_node.text(strip=True)
            if normalize_text(cand_loc) not in {"sem localizacao", "nao informado", ""}:
                location = cand_loc

    published_raw: Any = seed.get("published_at") or (ld.get("datePosted") if ld else None)
    if not published_raw:
        meta_pub = tree.css_first('meta[property="article:published_time"], meta[name="date"]')
        if meta_pub and meta_pub.attributes.get("content"):
            published_raw = meta_pub.attributes["content"]
    if not published_raw:
        time_el = tree.css_first("time[datetime], .posted-time-ago__text")
        if time_el:
            published_raw = time_el.attributes.get("datetime") or time_el.text(strip=True)
    if not published_raw:
        published_raw = _extract_relative_date_from_text(description[:800])

    published_iso = normalize_published_iso(published_raw)

    norm_html = normalize_text(raw_html)
    closed = bool(
        re.search(
            r"oferta fechada|esta lista de empregos expirou|vaga encerrada|vaga foi encerrada",
            norm_html,
        )
    )
    pdf_confirmed = bool(
        re.search(r"curr[ií]culo.{0,40}pdf|pdf.{0,40}curr[ií]culo|anex(?:e|ar).{0,30}curr", norm_html)
    )

    source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return RawJobInput(
        source=source_key,
        source_id=source_id,
        title=title,
        company=company,
        url=url,
        description=description[:80000],
        location=location,
        published_at=published_iso,
        opportunity_type=_infer_opportunity_type(title),  # type: ignore[arg-type]
        validity_status="encerrada" if closed else "aparentemente_aberta",
        pdf_confirmed=pdf_confirmed,
    )


class RssAndPagesConnector:
    def __init__(
        self,
        config: RssLocalSourceConfig | PublicPagesSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = f"{config.type}:{config.id}"

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = seen_source_ids or {}
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []
        candidates: dict[str, dict[str, Any]] = {}

        seeds = (
            self.config.seed_pages
            if isinstance(self.config, RssLocalSourceConfig)
            else self.config.pages
        )
        for s in seeds:
            candidates[s.url] = {
                "url": s.url,
                "title": s.title,
                "company": s.company,
                "location": s.location,
            }

        examined = 0
        if isinstance(self.config, RssLocalSourceConfig):
            for feed_url in self.config.feeds:
                try:
                    xml_text = await self.client.get_text(
                        feed_url, rate_limit_seconds=self.config.rate_limit_seconds
                    )
                    if "<!ENTITY" in xml_text or "<!DOCTYPE" in xml_text:
                        raise ConnectorError("External XML entity declarations rejected.")
                    root = ET.fromstring(xml_text)
                    for item in root.findall("./channel/item"):
                        examined += 1
                        title = unescape(item.findtext("title", "")).strip()
                        link = (item.findtext("link", "") or "").strip()
                        if not title or not link.startswith("https://"):
                            continue
                        if is_garbage_job_candidate(title, link):
                            continue
                        if not _match_catalog_areas(title, self.criteria) or _is_senior_title(
                            title, self.criteria
                        ):
                            continue
                        pub_raw = item.findtext("pubDate")
                        if pub_raw and not is_published_within_days(pub_raw, max_days=MAX_RECENCY_DAYS):
                            continue
                        pub_iso = normalize_published_iso(pub_raw)
                        # Extract inline content:encoded if available so even if detail page blocks, RSS description is rich
                        content_encoded = ""
                        for child in item:
                            if child.tag.endswith("encoded") and child.text:
                                content_encoded = html_to_clean_text(child.text)
                                break
                        rss_desc = content_encoded or html_to_clean_text(item.findtext("description", "") or "")
                        candidates.setdefault(
                            link,
                            {
                                "url": link,
                                "title": title,
                                "published_at": pub_iso,
                                "rss_description": rss_desc,
                                "location": "Pernambuco",
                            },
                        )
                except BudgetExhaustedError:
                    break
                except ConnectorError as exc:
                    errors.append({"url": feed_url, "error": str(exc)})

        by_area: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for url, seed in candidates.items():
            sid = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            if sid in seen:
                continue
            title = seed.get("title") or ""
            if title and _is_senior_title(title, self.criteria):
                continue
            area = next(iter(_match_catalog_areas(title, self.criteria)), "general")
            by_area[area].append((url, seed))

        selected: list[tuple[str, dict[str, Any]]] = []
        while by_area and len(selected) < self.config.max_items:
            for area in sorted(list(by_area.keys())):
                selected.append(by_area[area].pop(0))
                if not by_area[area]:
                    del by_area[area]
                if len(selected) >= self.config.max_items:
                    break

        for url, seed in selected:
            rss_desc = str(seed.get("rss_description") or "").strip()
            if len(rss_desc) >= 120 and seed.get("published_at"):
                jobs.append(
                    RawJobInput(
                        source=self.source_key,
                        source_id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
                        title=str(seed["title"]),
                        company=seed.get("company"),
                        url=url,
                        description=rss_desc[:80000],
                        location=seed.get("location"),
                        published_at=normalize_published_iso(seed.get("published_at")),
                        validity_status="aparentemente_aberta",
                    )
                )
                continue

            try:
                raw_html = await self.client.get_text(
                    url, rate_limit_seconds=self.config.rate_limit_seconds
                )
                job = _extract_html_job_page(raw_html, url, self.source_key, seed)
                if is_published_within_days(job.published_at, max_days=MAX_RECENCY_DAYS):
                    jobs.append(job)
            except BudgetExhaustedError:
                break
            except AccessRestrictedError as exc:
                errors.append({"url": url, "error": str(exc), "status_code": exc.status_code})
                if seed.get("title") and is_published_within_days(seed.get("published_at"), max_days=MAX_RECENCY_DAYS):
                    jobs.append(
                        RawJobInput(
                            source=self.source_key,
                            source_id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
                            title=seed["title"],
                            company=seed.get("company"),
                            url=url,
                            description=rss_desc or f"{seed['title']} - {seed.get('company') or ''}".strip(" -"),
                            location=seed.get("location"),
                            published_at=normalize_published_iso(seed.get("published_at")),
                            incomplete_summary=len(rss_desc) < 100,
                            access_blocked=True,
                        )
                    )
            except ConnectorError as exc:
                errors.append({"url": url, "error": str(exc)})

        stats = {
            "feed_items_examined": examined,
            "candidate_urls": len(candidates),
            "fetched_urls": len(selected),
            "extracted_jobs": len(jobs),
        }
        return jobs, errors, stats


class SitemapJsonLdConnector:
    def __init__(
        self,
        config: SitemapSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = f"sitemap_jsonld:{config.id}"

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = seen_source_ids or {}
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []
        effective_days = min(self.config.recency_days, MAX_RECENCY_DAYS)
        cutoff = datetime.now(timezone.utc) - timedelta(days=effective_days)

        slug_patterns = [f"-{slug.replace('_', '-')}" for slug in self.config.allowed_city_slugs]
        entries: list[tuple[str, str]] = []

        for sm_url in self.config.sitemaps:
            try:
                xml_text = await self.client.get_text(
                    sm_url, rate_limit_seconds=self.config.rate_limit_seconds
                )
                if "<!ENTITY" in xml_text or "<!DOCTYPE" in xml_text:
                    raise ConnectorError("External XML entity declarations rejected.")
                root = ET.fromstring(xml_text)
                for node in root:
                    if node.tag.split("}")[-1] != "url":
                        continue
                    loc_el = node.find("{*}loc")
                    mod_el = node.find("{*}lastmod")
                    if loc_el is None or not loc_el.text:
                        continue
                    loc = loc_el.text.strip()
                    lastmod = (mod_el.text or "").strip() if mod_el is not None else ""
                    if not any(pat in loc.lower() for pat in slug_patterns):
                        continue
                    if lastmod:
                        dt = parse_published_at_utc(lastmod)
                        if dt is not None and dt < cutoff:
                            continue
                    entries.append((lastmod, loc))
            except BudgetExhaustedError:
                break
            except ConnectorError as exc:
                errors.append({"url": sm_url, "error": str(exc)})

        entries.sort(reverse=True)
        for lastmod, url in entries:
            if len(jobs) >= self.config.max_items:
                break
            id_match = re.search(r"-(\d+)/?$", url)
            item_id = id_match.group(1) if id_match else hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            prev_mod = seen.get(item_id)
            if prev_mod and lastmod and lastmod <= prev_mod[:10]:
                continue
            try:
                raw_html = await self.client.get_text(
                    url, rate_limit_seconds=self.config.rate_limit_seconds
                )
                job = _extract_html_job_page(raw_html, url, self.source_key, {"published_at": lastmod})
                job.source_id = item_id
                job.updated_at_source = lastmod or None
                if is_published_within_days(job.published_at, max_days=effective_days):
                    jobs.append(job)
            except BudgetExhaustedError:
                break
            except AccessRestrictedError as exc:
                errors.append({"url": url, "error": str(exc), "status_code": exc.status_code})
                if exc.status_code in {401, 403, 429}:
                    break
            except ConnectorError as exc:
                errors.append({"url": url, "error": str(exc)})

        return jobs, errors, {"sitemap_candidates": len(entries), "extracted_jobs": len(jobs)}


class GreenhouseAndLeverConnector:
    def __init__(
        self,
        config: GreenhouseSourceConfig | LeverSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = f"{config.type}:{config.board_token}"
        self.base_url = (
            f"https://boards-api.greenhouse.io/v1/boards/{config.board_token}/jobs"
            if isinstance(config, GreenhouseSourceConfig)
            else f"https://api.lever.co/v0/postings/{config.board_token}"
        )

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        is_gh = isinstance(self.config, GreenhouseSourceConfig)
        list_url = self.base_url + ("?content=true" if is_gh else "?mode=json&limit=100")
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []

        try:
            payload = await self.client.get_json(
                list_url, rate_limit_seconds=self.config.rate_limit_seconds
            )
        except ConnectorError as exc:
            return [], [{"url": list_url, "error": str(exc)}], {"extracted_jobs": 0}

        rows = payload.get("jobs") if is_gh and isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return [], [{"url": list_url, "error": "Unexpected board JSON schema"}], {"extracted_jobs": 0}

        for raw in rows:
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("id") or "")
            title = str(raw.get("title" if is_gh else "text") or "").strip()
            url = str(raw.get("absolute_url" if is_gh else "hostedUrl") or "").strip()
            if not item_id or not title or not url.startswith("https://"):
                continue

            if self.config.entry_level_local_only:
                if _is_senior_title(title, self.criteria):
                    continue
                if not _match_catalog_areas(title, self.criteria):
                    continue

            if is_gh:
                description = html_to_clean_text(str(raw.get("content") or ""))
                loc_obj = raw.get("location")
                location = loc_obj.get("name") if isinstance(loc_obj, dict) else None
                workplace = None
                contract = None
                published_raw = raw.get("first_published") or raw.get("updated_at")
                opp_type = _infer_opportunity_type(title, raw.get("internal_job_id", 1))
            else:
                cats = raw.get("categories") or {}
                location = "; ".join(cats.get("allLocations") or [cats.get("location") or ""]) or None
                wp_raw = str(raw.get("workplaceType") or "")
                workplace = {"remote": "remoto", "hybrid": "hibrido", "on-site": "presencial"}.get(wp_raw)  # type: ignore[assignment]
                contract = cats.get("commitment")
                parts = [str(raw.get("descriptionPlain") or html_to_clean_text(str(raw.get("description") or "")))]
                for lst in raw.get("lists") or []:
                    if isinstance(lst, dict):
                        parts.append(f"{lst.get('text', '')}\n{html_to_clean_text(str(lst.get('content') or ''))}")
                description = "\n".join(p for p in parts if p.strip())
                published_raw = raw.get("createdAt")
                opp_type = _infer_opportunity_type(title)

            if not is_published_within_days(published_raw, max_days=MAX_RECENCY_DAYS):
                continue
            if len(description.strip()) < 20:
                continue

            jobs.append(
                RawJobInput(
                    source=self.source_key,
                    source_id=item_id,
                    title=title,
                    company=str(raw.get("company_name") or self.config.company_name),
                    url=url,
                    description=description[:80000],
                    location=location,
                    workplace_mode=workplace,  # type: ignore[arg-type]
                    contract_hint=contract,
                    published_at=normalize_published_iso(published_raw),
                    updated_at_source=raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
                    opportunity_type=opp_type,  # type: ignore[arg-type]
                    validity_status="confirmadamente_aberta",
                )
            )
            if len(jobs) >= self.config.max_items:
                break

        return jobs, errors, {"board_rows": len(rows), "extracted_jobs": len(jobs)}


class AshbyAndRecruiteeConnector:
    """Native JSON connector for Ashby (`api.ashbyhq.com`) and Recruitee (`*.recruitee.com/api/offers/`)."""

    def __init__(
        self,
        config: AshbySourceConfig | RecruiteeSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        is_ashby = isinstance(config, AshbySourceConfig)
        slug = config.board_slug if isinstance(config, AshbySourceConfig) else config.company_slug
        self.source_key = f"{config.type}:{slug}"
        self.api_url = (
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
            if is_ashby
            else f"https://{slug}.recruitee.com/api/offers/"
        )

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = seen_source_ids or {}
        is_ashby = isinstance(self.config, AshbySourceConfig)
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []

        try:
            payload = await self.client.get_json(
                self.api_url, rate_limit_seconds=self.config.rate_limit_seconds
            )
        except ConnectorError as exc:
            return [], [{"url": self.api_url, "error": str(exc)}], {"extracted_jobs": 0}

        rows = (
            payload.get("jobs" if is_ashby else "offers")
            if isinstance(payload, dict)
            else []
        )
        if not isinstance(rows, list):
            return [], errors, {"extracted_jobs": 0}

        for raw in rows:
            if len(jobs) >= self.config.max_items:
                break
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("id") or "")
            title = str(raw.get("title") or "").strip()
            url = str(raw.get("jobUrl" if is_ashby else "careers_url") or "").strip()
            if not item_id or not title or not url.startswith("https://") or item_id in seen:
                continue
            if self.config.entry_level_local_only:
                if _is_senior_title(title, self.criteria) or not _match_catalog_areas(title, self.criteria):
                    continue

            pub_raw = raw.get("publishedAt" if is_ashby else "published_at") or raw.get("created_at")
            if not is_published_within_days(pub_raw, max_days=MAX_RECENCY_DAYS):
                continue

            is_remote = bool(raw.get("isRemote" if is_ashby else "remote"))
            location = str(raw.get("location" if is_ashby else "city") or "").strip() or (
                "Remoto" if is_remote else None
            )
            desc_raw = str(raw.get("descriptionHtml" if is_ashby else "description") or "")
            desc = html_to_clean_text(desc_raw) or f"{title} — {self.config.company_name}"

            jobs.append(
                RawJobInput(
                    source=self.source_key,
                    source_id=item_id,
                    title=title,
                    company=self.config.company_name,
                    url=url,
                    description=desc[:80000],
                    location=location,
                    workplace_mode="remoto" if is_remote else None,
                    published_at=normalize_published_iso(pub_raw),
                    opportunity_type=_infer_opportunity_type(title),  # type: ignore[arg-type]
                    validity_status="confirmadamente_aberta",
                )
            )

        return jobs, errors, {"board_rows": len(rows), "extracted_jobs": len(jobs)}


class GupyApiConnector:
    """Fast async connector for Gupy via SSR `<script id="__NEXT_DATA__">` on `portal.gupy.io` & company boards."""

    def __init__(
        self,
        config: GupyApiSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = f"gupy_api:{config.id}"

    @staticmethod
    def _extract_next_data_jobs(raw_text: str) -> list[dict[str, Any]]:
        if not raw_text:
            return []
        stripped = raw_text.lstrip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
                    return [x for x in parsed["data"] if isinstance(x, dict)]
            except ValueError:
                pass

        tree = LexborHTMLParser(raw_text)
        nd = tree.css_first("script#__NEXT_DATA__")
        if nd is None:
            return []
        try:
            payload = json.loads(nd.text(deep=True))
        except ValueError:
            return []
        page_props = (payload.get("props") or {}).get("pageProps") or {}
        if isinstance(page_props.get("jobs"), list):
            return [x for x in page_props["jobs"] if isinstance(x, dict)]
        init_list = page_props.get("initialJobList")
        if isinstance(init_list, dict) and isinstance(init_list.get("data"), list):
            return [x for x in init_list["data"] if isinstance(x, dict)]
        return []

    def _matches_target_company(self, raw_career_page_name: str) -> bool:
        target = self.config.career_page_name or self.config.company_name
        if not target:
            return True
        norm_got = normalize_text(raw_career_page_name)
        norm_want = normalize_text(target)
        if not norm_got:
            return False
        return norm_want == norm_got or norm_want in norm_got or norm_got in norm_want

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = seen_source_ids or {}
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []
        found_by_id: dict[str, dict[str, Any]] = {}

        urls_to_fetch: list[str] = []
        if self.config.career_subdomain:
            urls_to_fetch.append(f"https://{self.config.career_subdomain.lower()}.gupy.io")
        elif self.config.career_page_name:
            inferred_sub = re.sub(r"[^a-z0-9]", "", self.config.career_page_name.lower())
            if inferred_sub:
                urls_to_fetch.append(f"https://{inferred_sub}.gupy.io")
            urls_to_fetch.append(
                f"https://portal.gupy.io/job-search/term={quote(self.config.career_page_name)}"
            )
        else:
            terms = self.config.queries or [
                "suporte Recife",
                "desenvolvedor Recife",
                "dados Recife",
                "administrativo Recife",
                "suporte ti junior",
                "desenvolvedor junior",
            ]
            for term in terms:
                urls_to_fetch.append(f"https://portal.gupy.io/job-search/term={quote(term)}")

        for url in urls_to_fetch:
            try:
                raw_html = await self.client.get_text(
                    url,
                    check_robots=False,
                    allow_404_empty=True,
                    rate_limit_seconds=self.config.rate_limit_seconds,
                )
                for item in self._extract_next_data_jobs(raw_html):
                    jid = str(item.get("id") or "")
                    if not jid:
                        continue
                    c_name = str(item.get("careerPageName") or self.config.company_name or "")
                    if not self._matches_target_company(c_name):
                        continue
                    found_by_id[jid] = item
            except BudgetExhaustedError:
                break
            except ConnectorError as exc:
                errors.append({"url": url, "error": str(exc)})

        is_company_board = bool(self.config.career_page_name or self.config.career_subdomain)
        for item_id, raw in found_by_id.items():
            if len(jobs) >= self.config.max_items:
                break
            if item_id in seen:
                continue
            title = str(raw.get("name") or raw.get("title") or "").strip()
            job_url = str(raw.get("jobUrl") or "").strip()
            if not job_url and self.config.career_subdomain:
                job_url = f"https://{self.config.career_subdomain.lower()}.gupy.io/jobs/{item_id}"
            if not title or not job_url.startswith("https://"):
                continue
            if _is_senior_title(title, self.criteria):
                continue

            # For big multi-role company boards (e.g. Moura / QCA), pre-filter to catalog areas or PE/Remote
            city = str(raw.get("city") or "").strip()
            state = str(raw.get("state") or "").strip()
            is_remote = bool(raw.get("isRemoteWork"))
            workplace_type = normalize_text(str(raw.get("workplaceType") or ""))
            is_rem = is_remote or workplace_type == "remote"

            if is_company_board and not _match_catalog_areas(title, self.criteria):
                continue
            if state and normalize_text(state) not in {"pe", "pernambuco"} and not is_rem:
                continue

            pub_raw = raw.get("publishedDate") or raw.get("updatedAt")
            if not is_published_within_days(pub_raw, max_days=MAX_RECENCY_DAYS, allow_missing=is_company_board):
                continue

            company = str(
                raw.get("careerPageName") or self.config.company_name or ""
            ).strip() or None
            workplace = (
                "remoto"
                if is_rem
                else "hibrido"
                if "hybrid" in workplace_type or "hibrid" in workplace_type
                else "presencial"
                if city
                else None
            )
            loc_str = " - ".join(x for x in [city, state] if x) or ("Remoto" if is_rem else None)
            desc_raw = str(raw.get("description") or "")
            desc = html_to_clean_text(desc_raw) if desc_raw else f"{title} — {company or 'Gupy'}"
            if len(desc) < 25:
                desc = f"{title} — {company or 'Gupy'} ({loc_str or 'Brasil'})"

            jobs.append(
                RawJobInput(
                    source=self.source_key,
                    source_id=item_id,
                    title=title,
                    company=company,
                    url=job_url,
                    description=desc[:80000],
                    location=loc_str,
                    workplace_mode=workplace,  # type: ignore[arg-type]
                    contract_hint=str(raw.get("type") or "") or None,
                    published_at=normalize_published_iso(pub_raw),
                    opportunity_type=_infer_opportunity_type(title),  # type: ignore[arg-type]
                    incomplete_summary=len(desc_raw) < 80,
                    requires_login=True,
                    validity_status="confirmadamente_aberta",
                )
            )

        return jobs, errors, {"gupy_candidates": len(found_by_id), "extracted_jobs": len(jobs)}


class GitHubIssuesConnector:
    """Fast async connector for Brazilian community tech job boards on GitHub Issues."""

    def __init__(
        self,
        config: GitHubIssuesSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = f"github_issues:{config.id}"

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = seen_source_ids or {}
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []
        total_issues = 0

        for repo in self.config.repos:
            if len(jobs) >= self.config.max_items:
                break
            api_url = (
                f"https://api.github.com/repos/{repo}/issues"
                f"?state=open&sort=created&direction=desc&per_page={self.config.per_repo}"
            )
            try:
                rows = await self.client.get_json(api_url, rate_limit_seconds=self.config.rate_limit_seconds)
            except BudgetExhaustedError:
                break
            except ConnectorError as exc:
                errors.append({"url": api_url, "error": str(exc)})
                continue

            if not isinstance(rows, list):
                continue

            for issue in rows:
                if len(jobs) >= self.config.max_items:
                    break
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                total_issues += 1
                issue_id = f"{repo}#{issue.get('number') or issue.get('id')}"
                if issue_id in seen:
                    continue

                created_at = issue.get("created_at")
                if not is_published_within_days(created_at, max_days=MAX_RECENCY_DAYS, allow_missing=False):
                    continue

                raw_title = str(issue.get("title") or "").strip()
                html_url = str(issue.get("html_url") or "").strip()
                body = str(issue.get("body") or "").strip()
                if not raw_title or not html_url.startswith("https://"):
                    continue
                if GITHUB_META_ISSUE_RE.search(raw_title) or _is_senior_title(raw_title, self.criteria):
                    continue

                bracket_locs = re.findall(r"\[([^\]]+)\]", raw_title)
                clean_title = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", raw_title).strip()
                company: str | None = None
                if " @ " in clean_title:
                    clean_title, company = [p.strip() for p in clean_title.split(" @ ", 1)]
                elif " | " in clean_title:
                    parts = [p.strip() for p in clean_title.split(" | ", 1)]
                    if len(parts[1]) <= 50:
                        clean_title, company = parts[0], parts[1]
                elif re.search(r"\s+na\s+[A-ZÀ-Ý]", clean_title):
                    parts = re.split(r"\s+na\s+", clean_title, maxsplit=1)
                    if len(parts) == 2 and len(parts[1]) <= 60:
                        clean_title, company = parts[0].strip(), parts[1].strip()

                if not company and body:
                    m_co = re.search(r"(?im)^(?:\*\*|#+\s*)?empresa(?:\*\*)?\s*[:\-]\s*([^\n*#]{2,60})", body)
                    if m_co:
                        company = m_co.group(1).strip()

                labels = [
                    str(lb.get("name") or "")
                    for lb in (issue.get("labels") or [])
                    if isinstance(lb, dict)
                ]
                label_blob = " ".join([*bracket_locs, *labels, raw_title])
                norm_labels = normalize_text(label_blob)
                workplace = (
                    "remoto"
                    if any(w in norm_labels for w in ("remoto", "remote", "home office", "home-office"))
                    else "hibrido"
                    if "hibrido" in norm_labels
                    else "presencial"
                    if bracket_locs
                    else None
                )
                location = (
                    ", ".join(bracket_locs)
                    if bracket_locs
                    else ("Remoto" if workplace == "remoto" else None)
                )
                if workplace != "remoto" and location and not any(
                    c in normalize_text(location) for c in ("recife", "pernambuco", "pe", "jaboatao", "remoto")
                ):
                    continue

                desc = f"{raw_title}\nLabels: {', '.join(labels)}\n\n{body}".strip()
                jobs.append(
                    RawJobInput(
                        source=self.source_key,
                        source_id=issue_id,
                        title=clean_title or raw_title,
                        company=company,
                        url=html_url,
                        description=desc[:80000],
                        location=location,
                        workplace_mode=workplace,  # type: ignore[arg-type]
                        published_at=normalize_published_iso(created_at),
                        validity_status="confirmadamente_aberta",
                    )
                )

        return jobs, errors, {"issues_examined": total_issues, "extracted_jobs": len(jobs)}


class InhireApiConnector:
    """Fast async connector for public Inhire career boards (`X-Tenant` + `jobsPage`)."""

    def __init__(
        self,
        config: InhireSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = f"inhire:{config.tenant_slug}"

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = seen_source_ids or {}
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []
        api_url = "https://api.inhire.app/job-posts/public/pages"

        try:
            payload = await self.client.get_json(
                api_url,
                rate_limit_seconds=self.config.rate_limit_seconds,
                extra_headers={"X-Tenant": self.config.tenant_slug},
            )
        except ConnectorError as exc:
            errors.append({"url": api_url, "tenant": self.config.tenant_slug, "error": str(exc)})
            return [], errors, {"extracted_jobs": 0}

        rows = (
            payload.get("jobsPage") or payload.get("jobs") or payload.get("data") or payload.get("items")
            if isinstance(payload, dict)
            else payload
        )
        if not isinstance(rows, list):
            return [], errors, {"extracted_jobs": 0}

        base_career = (self.config.career_url or f"https://{self.config.tenant_slug}.inhire.app").rstrip("/")
        for raw in rows:
            if len(jobs) >= self.config.max_items:
                break
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("jobId") or raw.get("id") or raw.get("slug") or "")
            title = str(raw.get("displayName") or raw.get("title") or "").strip()
            if not item_id or not title or item_id in seen:
                continue
            if _is_senior_title(title, self.criteria):
                continue
            pub_raw = raw.get("publishedAt") or raw.get("createdAt") or raw.get("updatedAt")
            if pub_raw and not is_published_within_days(pub_raw, max_days=MAX_RECENCY_DAYS):
                continue

            url = str(raw.get("url") or f"{base_career}/vagas/{item_id}").strip()
            if not url.startswith("https://"):
                continue
            wp_raw = normalize_text(str(raw.get("workplaceType") or ""))
            workplace = (
                "remoto"
                if "remote" in wp_raw or "remoto" in wp_raw
                else "hibrido"
                if "hybrid" in wp_raw or "hibrid" in wp_raw
                else "presencial"
                if wp_raw
                else None
            )
            loc = str(raw.get("location") or raw.get("workplaceLocation") or "").strip() or (
                "Remoto" if workplace == "remoto" else None
            )
            desc = (
                html_to_clean_text(str(raw.get("description") or ""))
                or f"{title} — {self.config.company_name} ({loc or 'Recife / Remoto'})"
            )

            jobs.append(
                RawJobInput(
                    source=self.source_key,
                    source_id=item_id,
                    title=title,
                    company=self.config.company_name,
                    url=url,
                    description=desc[:80000],
                    location=loc,
                    workplace_mode=workplace,  # type: ignore[arg-type]
                    published_at=normalize_published_iso(pub_raw),
                    opportunity_type=_infer_opportunity_type(title),  # type: ignore[arg-type]
                    incomplete_summary=len(desc) < 80,
                    validity_status="confirmadamente_aberta",
                )
            )

        return jobs, errors, {"board_rows": len(rows), "extracted_jobs": len(jobs)}


class WorkableApiConnector:
    """Fast async connector for public Workable career widget API."""

    def __init__(
        self,
        config: WorkableSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = f"workable:{config.account_slug}"

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = seen_source_ids or {}
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []
        api_url = f"https://apply.workable.com/api/v1/widget/accounts/{self.config.account_slug}?details=true"

        try:
            payload = await self.client.get_json(api_url, rate_limit_seconds=self.config.rate_limit_seconds)
        except ConnectorError as exc:
            errors.append({"url": api_url, "error": str(exc)})
            return [], errors, {"extracted_jobs": 0}

        rows = payload.get("jobs") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return [], errors, {"extracted_jobs": 0}

        for raw in rows:
            if len(jobs) >= self.config.max_items:
                break
            if not isinstance(raw, dict):
                continue
            shortcode = str(raw.get("shortcode") or raw.get("id") or "")
            title = str(raw.get("title") or "").strip()
            url = str(raw.get("url") or "").strip()
            if not shortcode or not title or not url.startswith("https://") or shortcode in seen:
                continue
            if _is_senior_title(title, self.criteria):
                continue
            pub_raw = raw.get("published_on") or raw.get("created_at")
            if not is_published_within_days(pub_raw, max_days=MAX_RECENCY_DAYS):
                continue

            city = str(raw.get("city") or "").strip()
            state = str(raw.get("state") or "").strip()
            telecommuting = bool(raw.get("telecommuting"))
            loc = " - ".join(x for x in [city, state] if x) or ("Remoto" if telecommuting else None)
            desc = html_to_clean_text(str(raw.get("description") or "")) or f"{title} — {self.config.company_name}"

            jobs.append(
                RawJobInput(
                    source=self.source_key,
                    source_id=shortcode,
                    title=title,
                    company=self.config.company_name,
                    url=url,
                    description=desc[:80000],
                    location=loc,
                    workplace_mode="remoto" if telecommuting else None,
                    published_at=normalize_published_iso(pub_raw),
                    validity_status="confirmadamente_aberta",
                )
            )

        return jobs, errors, {"extracted_jobs": len(jobs)}


class HtmlJobBoardConnector:
    """Fast async connector supporting LinkedIn `jobs-guest` `.base-search-card` + detail API,
    native JSON endpoints for `Remotar` and `Trampos.co`, Jobvite (`Liferay`), and verified
    individual job cards across Vagas.com.br, Catho, InfoJobs, BNE, Programathor, GeekHunter, and DivulgaVagas."""

    def __init__(
        self,
        config: HtmlBoardSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = f"html_board:{config.id}"

    async def _discover_remotar_json(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cards: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for term in ("suporte", "junior", "dados"):
            api_url = f"https://api.remotar.com.br/jobs?search={quote_plus(term)}"
            try:
                payload = await self.client.get_json(api_url, rate_limit_seconds=self.config.rate_limit_seconds)
                rows = payload.get("data") if isinstance(payload, dict) else []
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict) or not row.get("id"):
                        continue
                    jid = str(row["id"])
                    title = str(row.get("title") or "").strip()
                    comp_obj = row.get("company")
                    comp = comp_obj.get("name") if isinstance(comp_obj, dict) else None
                    pub = row.get("publishedAt") or row.get("createdAt") or row.get("updatedAt")
                    desc = html_to_clean_text(str(row.get("description") or ""))
                    cards.append(
                        {
                            "title": title,
                            "company": comp,
                            "location": "Remoto",
                            "workplace_mode": "remoto",
                            "url": f"https://remotar.com.br/job/{jid}",
                            "published_at": pub,
                            "snippet": desc or title,
                            "verified_feed": True,
                        }
                    )
            except BudgetExhaustedError:
                break
            except ConnectorError as exc:
                errors.append({"url": api_url, "error": str(exc)})
        return cards, errors

    async def _discover_trampos_json(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cards: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        api_url = "https://trampos.co/api/v2/opportunities?ct=ti-desenvolvimento-programacao"
        try:
            payload = await self.client.get_json(api_url, rate_limit_seconds=self.config.rate_limit_seconds)
            rows = (
                payload.get("opportunities") or payload.get("collection")
                if isinstance(payload, dict)
                else []
            )
            if isinstance(rows, list):
                for entry in rows:
                    raw = entry.get("opportunity") if isinstance(entry, dict) and isinstance(entry.get("opportunity"), dict) else entry
                    if not isinstance(raw, dict) or not raw.get("id"):
                        continue
                    jid = str(raw["id"])
                    title = str(raw.get("name") or raw.get("title") or "").strip()
                    comp_obj = raw.get("company")
                    comp = (
                        comp_obj.get("name")
                        if isinstance(comp_obj, dict)
                        else str(raw.get("company_name") or "").strip() or None
                    )
                    city = str(raw.get("city") or "").strip()
                    state = str(raw.get("state") or "").strip()
                    is_rem = bool(raw.get("home_office") or raw.get("remote"))
                    if state and normalize_text(state) not in {"pe", "pernambuco"} and not is_rem:
                        continue
                    loc = " - ".join(x for x in [city, state] if x) or ("Remoto" if is_rem else None)
                    pub = raw.get("published_at") or raw.get("created_at")
                    desc = html_to_clean_text(str(raw.get("description") or raw.get("summary") or ""))
                    cards.append(
                        {
                            "title": title,
                            "company": comp,
                            "location": loc,
                            "workplace_mode": "remoto" if is_rem else None,
                            "url": f"https://trampos.co/oportunidades/{jid}",
                            "published_at": pub,
                            "snippet": desc or title,
                            "verified_feed": True,
                        }
                    )
        except ConnectorError as exc:
            errors.append({"url": api_url, "error": str(exc)})
        return cards, errors

    def _parse_linkedin_cards(self, tree: LexborHTMLParser, base_url: str) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        nodes = tree.css(".base-search-card")
        if not nodes:
            nodes = tree.css(".base-card")

        for node in nodes:
            title_el = node.css_first(".base-search-card__title, h3")
            link_el = node.css_first("a.base-card__full-link, a[href*='/jobs/view/']")
            if not title_el or not link_el:
                continue
            href = (link_el.attributes.get("href") or "").strip()
            full_url = urljoin(base_url, href).split("?", 1)[0]
            if not full_url.startswith("https://") or full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            urn = node.attributes.get("data-entity-urn") or ""
            m_urn = re.search(r"jobPosting:(\d+)", urn) or re.search(r"-(\d+)/?$", full_url)
            linkedin_id = m_urn.group(1) if m_urn else None

            comp_el = node.css_first(".base-search-card__subtitle, h4")
            loc_el = node.css_first(".job-search-card__location")
            time_el = node.css_first("time")
            pub_raw = (
                (time_el.attributes.get("datetime") if time_el else None)
                or (time_el.text(strip=True) if time_el else None)
            )
            loc_txt = loc_el.text(strip=True) if loc_el else self.config.location_fallback
            is_rem_query = "f_wt=2" in base_url.lower()
            cards.append(
                {
                    "linkedin_job_id": linkedin_id,
                    "title": title_el.text(strip=True),
                    "company": comp_el.text(strip=True) if comp_el else self.config.company_fallback,
                    "location": "Remoto - Brasil" if is_rem_query and not loc_txt else loc_txt,
                    "workplace_mode": "remoto" if is_rem_query else None,
                    "url": full_url,
                    "published_at": pub_raw,
                    "snippet": node.text(separator=" ", strip=True),
                    "verified_feed": True,
                }
            )
        return cards

    def _parse_jobvite_cards(self, tree: LexborHTMLParser, base_url: str) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for a_node in tree.css("a[href*='/job/']"):
            href = (a_node.attributes.get("href") or "").strip()
            full_url = urljoin(base_url, href).split("?", 1)[0]
            title = a_node.text(strip=True)
            if not full_url.startswith("https://") or full_url in seen_urls:
                continue
            if is_garbage_job_candidate(title, full_url):
                continue
            row = a_node.parent.parent if a_node.parent and a_node.parent.parent else a_node.parent
            row_txt = row.text(separator=" | ", strip=True) if row else ""
            norm_row = normalize_text(row_txt)
            # Keep only Recife, Pernambuco, Brazil, or Remote Latin America roles
            if not any(tok in norm_row for tok in ("recife", "brazil", "brasil", "pernambuco", "remote")):
                continue
            seen_urls.add(full_url)
            wp = "remoto" if "remote" in norm_row else "presencial"
            loc = "Recife - PE" if "recife" in norm_row else ("Remoto - Brasil" if wp == "remoto" else "Brasil")
            cards.append(
                {
                    "title": title,
                    "company": self.config.company_fallback or "Liferay",
                    "location": loc,
                    "workplace_mode": wp,
                    "url": full_url,
                    "published_at": None,
                    "snippet": row_txt or title,
                    "verified_feed": True,
                }
            )
        return cards

    def _parse_generic_cards(self, raw_html: str, tree: LexborHTMLParser, base_url: str) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        base_host = (urlsplit(base_url).hostname or "").lower().removeprefix("www.")

        if base_host == "jobs.jobvite.com":
            return self._parse_jobvite_cards(tree, base_url)

        # 1. Try JSON-LD JobPosting / ItemList if embedded on the search page
        for script_node in tree.css('script[type="application/ld+json"]'):
            txt = script_node.text(deep=True).strip()
            if not txt:
                continue
            try:
                ld_data = json.loads(txt)
            except ValueError:
                continue
            items = (
                ld_data.get("itemListElement")
                if isinstance(ld_data, dict) and isinstance(ld_data.get("itemListElement"), list)
                else ld_data
                if isinstance(ld_data, list)
                else [ld_data]
            )
            for entry in items:
                obj = entry.get("item") if isinstance(entry, dict) and isinstance(entry.get("item"), dict) else entry
                if not isinstance(obj, dict):
                    continue
                types = obj.get("@type")
                t_list = types if isinstance(types, list) else [types]
                if not any(isinstance(tp, str) and "JobPosting" in tp for tp in t_list):
                    continue
                u = str(obj.get("url") or "").strip()
                t = str(obj.get("title") or obj.get("name") or "").strip()
                if u.startswith("https://") and t and u not in seen_urls and not is_garbage_job_candidate(t, u):
                    seen_urls.add(u)
                    org = obj.get("hiringOrganization")
                    comp = org.get("name") if isinstance(org, dict) else self.config.company_fallback
                    cards.append(
                        {
                            "title": html_to_clean_text(t),
                            "company": comp,
                            "location": self.config.location_fallback,
                            "url": u,
                            "published_at": obj.get("datePosted"),
                            "snippet": html_to_clean_text(str(obj.get("description") or t)),
                            "verified_feed": True,
                        }
                    )

        # 2. Scan verified individual job anchors in DOM
        is_recent_sorted_feed = any(
            sig in base_url.lower()
            for sig in ("mais_recentes", "recife", "vagas-de-emprego-em-recife", "jobs-city/recife")
        )
        for a_node in tree.css("a[href]"):
            href = (a_node.attributes.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            full_url = urljoin(base_url, href).split("#", 1)[0]
            parsed_u = urlsplit(full_url)
            if parsed_u.scheme != "https" or not parsed_u.hostname:
                continue
            target_host = parsed_u.hostname.lower().removeprefix("www.")
            if target_host != base_host and "gupy.io" not in target_host and "inhire.app" not in target_host:
                continue
            if full_url.rstrip("/") == base_url.rstrip("/") or full_url in seen_urls:
                continue
            if not JOB_LINK_HINT_RE.search(parsed_u.path + ("?" + parsed_u.query if parsed_u.query else "")):
                continue
            if not is_verified_individual_job_url(full_url):
                continue

            card_node = a_node.parent
            if card_node is not None and len(card_node.text(strip=True)) < 35 and card_node.parent is not None:
                card_node = card_node.parent

            heading_el = (
                a_node.css_first("h2, h3, h4, .cell-list-content h3, strong")
                or (card_node.css_first("h2, h3, h4") if card_node else None)
            )
            title = (heading_el.text(strip=True) if heading_el else a_node.text(strip=True)).strip()
            title = re.sub(r"^📍\s*PRESENCIAL[^A-Za-zÀ-ÿ]*", "", title).strip()
            if is_garbage_job_candidate(title, full_url):
                continue

            seen_urls.add(full_url)
            card_text = card_node.text(separator=" | ", strip=True) if card_node else title
            time_el = card_node.css_first("time, .data-publicacao") if card_node else None
            pub_raw = (
                (time_el.attributes.get("datetime") if time_el else None)
                or (time_el.text(strip=True) if time_el else None)
                or _extract_relative_date_from_text(card_text)
            )
            cards.append(
                {
                    "title": title,
                    "company": self.config.company_fallback,
                    "location": self.config.location_fallback,
                    "url": full_url,
                    "published_at": pub_raw,
                    "snippet": card_text[:2000],
                    "verified_feed": is_recent_sorted_feed,
                }
            )
        return cards

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = seen_source_ids or {}
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []
        raw_cards: list[dict[str, Any]] = []

        first_host = (urlsplit(self.config.search_urls[0]).hostname or "").lower().removeprefix("www.")
        if first_host == "remotar.com.br":
            c_rem, e_rem = await self._discover_remotar_json()
            raw_cards.extend(c_rem)
            errors.extend(e_rem)
        elif first_host == "trampos.co":
            c_tr, e_tr = await self._discover_trampos_json()
            raw_cards.extend(c_tr)
            errors.extend(e_tr)
        else:
            for s_url in self.config.search_urls:
                if len(raw_cards) >= self.config.max_items * 3:
                    break
                is_linkedin = "linkedin.com" in s_url.lower()
                try:
                    raw_html = await self.client.get_text(
                        s_url,
                        check_robots=not is_linkedin,
                        rate_limit_seconds=self.config.rate_limit_seconds,
                    )
                    tree = LexborHTMLParser(raw_html)
                    extracted = (
                        self._parse_linkedin_cards(tree, s_url)
                        if is_linkedin
                        else self._parse_generic_cards(raw_html, tree, s_url)
                    )
                    raw_cards.extend(extracted)
                except BudgetExhaustedError:
                    break
                except ConnectorError as exc:
                    errors.append({"url": s_url, "error": str(exc)})

        linkedin_details_fetched = 0
        for card in raw_cards:
            if len(jobs) >= self.config.max_items:
                break
            url = str(card.get("url") or "").strip()
            title = str(card.get("title") or "").strip()
            if not url or not title or is_garbage_job_candidate(title, url):
                continue
            sid = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            if sid in seen:
                continue
            if _is_senior_title(title, self.criteria):
                continue

            # Filter generic national feeds down to catalog areas when not a dedicated company board
            if not self.config.company_fallback and not _match_catalog_areas(title, self.criteria):
                continue

            pub_raw = card.get("published_at")
            has_date = pub_raw is not None and str(pub_raw).strip() != ""
            if has_date and not is_published_within_days(pub_raw, max_days=MAX_RECENCY_DAYS, allow_missing=False):
                continue
            if not has_date and not card.get("verified_feed") and not self.config.fetch_details:
                # Discard static undated links lacking recency proof
                continue

            # Selective LinkedIn guest detail fetch on matching roles (fast, public fragment)
            li_id = card.get("linkedin_job_id")
            if li_id and self.config.fetch_details and linkedin_details_fetched < 5:
                detail_api = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{li_id}"
                try:
                    li_html = await self.client.get_text(
                        detail_api,
                        check_robots=False,
                        allow_404_empty=True,
                        rate_limit_seconds=self.config.rate_limit_seconds,
                    )
                    if li_html:
                        linkedin_details_fetched += 1
                        li_tree = LexborHTMLParser(li_html)
                        markup = li_tree.css_first(".show-more-less-html__markup, .description__text")
                        if markup:
                            card["snippet"] = html_to_clean_text(markup.html or "")
                            card["detail_complete"] = True
                except BudgetExhaustedError:
                    pass
                except ConnectorError:
                    pass
            elif self.config.fetch_details and not li_id:
                try:
                    detail_html = await self.client.get_text(
                        url, rate_limit_seconds=self.config.rate_limit_seconds
                    )
                    detail_job = _extract_html_job_page(detail_html, url, self.source_key, card)
                    if detail_job.published_at and not is_published_within_days(
                        detail_job.published_at, max_days=MAX_RECENCY_DAYS, allow_missing=False
                    ):
                        continue
                    jobs.append(detail_job)
                    continue
                except BudgetExhaustedError:
                    break
                except ConnectorError:
                    pass

            snippet = str(card.get("snippet") or title)
            desc = (
                snippet
                if card.get("detail_complete") or len(snippet) > 220
                else f"{title}\nEmpresa: {card.get('company') or 'Não informada'}\n{snippet}".strip()
            )
            jobs.append(
                RawJobInput(
                    source=self.source_key,
                    source_id=sid,
                    title=title,
                    company=card.get("company"),
                    url=url,
                    description=desc[:40000],
                    location=card.get("location"),
                    workplace_mode=card.get("workplace_mode"),  # type: ignore[arg-type]
                    published_at=normalize_published_iso(pub_raw) if has_date else None,
                    opportunity_type=_infer_opportunity_type(title),  # type: ignore[arg-type]
                    incomplete_summary=not card.get("detail_complete") and len(snippet) < 140,
                    requires_login=self.config.requires_login_for_apply,
                    validity_status="aparentemente_aberta",
                )
            )

        return jobs, errors, {"cards_found": len(raw_cards), "extracted_jobs": len(jobs)}


def _derive_empregare_queries(criteria: SearchCriteria, max_queries: int) -> list[dict[str, str]]:
    city_lookup = {
        normalize_text(alias): alias
        for city in criteria.geography.allowed_cities
        for alias in city.aliases
    }
    informal_set = {normalize_text(s) for s in criteria.role_catalog.informal_hiring_signals}
    queries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for group_expr in criteria.role_catalog.grouped_search_queries:
        groups = [
            [tok.strip() for tok in grp.split(" OR ") if tok.strip()]
            for grp in re.findall(r"\(([^)]*)\)", group_expr)
        ]
        groups = [g for g in groups if g]
        if len(groups) < 2 or any(normalize_text(a) in informal_set for a in groups[0]):
            continue
        outside_parens = re.sub(r"\([^)]*\)", " ", group_expr)
        is_remote = bool(re.search(r"\bremoto\b|\bbrasil\b", outside_parens, re.IGNORECASE))
        locations = (
            ["Remoto"]
            if is_remote
            else [city_lookup[normalize_text(a)] for a in groups[-1] if normalize_text(a) in city_lookup]
        )
        query_text = " ".join(
            g[0] for g in (groups[:-1] if locations and not is_remote else groups)
        )[:100]
        for city in locations:
            key = (normalize_text(query_text), normalize_text(city))
            if key not in seen and query_text:
                seen.add(key)
                queries.append({"query": query_text, "city": city, "remote_only": "1" if is_remote else ""})
                if len(queries) >= max_queries:
                    return queries
    return queries


class EmpregareMcpConnector:
    def __init__(
        self,
        config: EmpregareMcpSourceConfig,
        client: SafeAsyncHttpClient,
        criteria: SearchCriteria,
    ) -> None:
        self.config = config
        self.client = client
        self.criteria = criteria
        self.source_key = "empregare:mcp-publico"

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        seen = set((seen_source_ids or {}).keys())
        jobs: list[RawJobInput] = []
        errors: list[dict[str, Any]] = []

        try:
            await self.client.post_empregare_mcp_readonly(
                self.config.endpoint,
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "Resumatch-PublicDiscovery", "version": "2.2.0"},
                },
                rate_limit_seconds=self.config.rate_limit_seconds,
            )
            tools_res = await self.client.post_empregare_mcp_readonly(
                self.config.endpoint,
                "tools/list",
                rate_limit_seconds=self.config.rate_limit_seconds,
            )
            tools = tools_res.get("tools") or []
            if not any(isinstance(t, dict) and t.get("name") == "buscar_vagas" for t in tools):
                raise ConnectorError("Public tool 'buscar_vagas' unavailable on Empregare MCP.")
        except ConnectorError as exc:
            return [], [{"url": self.config.endpoint, "error": str(exc)}], {"extracted_jobs": 0}

        queries = _derive_empregare_queries(self.criteria, self.config.max_queries)
        found_items: dict[str, tuple[dict[str, Any], bool]] = {}

        for q in queries:
            is_rem_q = bool(q.get("remote_only"))
            loc_arg = "Brasil" if is_rem_q else q["city"]
            try:
                call_res = await self.client.post_empregare_mcp_readonly(
                    self.config.endpoint,
                    "tools/call",
                    {
                        "name": "buscar_vagas",
                        "arguments": {
                            "query": q["query"],
                            "localidade": loc_arg,
                            "pagina": 1,
                            "itensPagina": 6,
                        },
                    },
                    rate_limit_seconds=self.config.rate_limit_seconds,
                )
                text_block = next(
                    (c.get("text") for c in (call_res.get("content") or []) if isinstance(c, dict) and c.get("type") == "text"),
                    None,
                )
                parsed = json.loads(text_block) if text_block else {}
                for v in parsed.get("vagas") or []:
                    if isinstance(v, dict) and v.get("id"):
                        found_items[str(v["id"])] = (v, is_rem_q)
            except BudgetExhaustedError:
                break
            except (ConnectorError, ValueError) as exc:
                errors.append({"query": q, "error": str(exc)})

        allowed_city_norms = {
            normalize_text(alias)
            for c in self.criteria.geography.allowed_cities
            for alias in [c.canonical_name, *c.aliases]
        }

        stop_details = False
        for item_id, (item, is_rem_q) in list(found_items.items())[: self.config.max_items * 2]:
            if len(jobs) >= self.config.max_items:
                break
            if item_id in seen:
                continue
            title = str(item.get("cargo") or "").strip()
            company = str(item.get("empresa") or "").strip() or None
            location = "; ".join(item.get("cidades") or []) or None
            remoto_val = str(item.get("remoto") or "")
            wp_map = {"Presencial": "presencial", "Remoto": "remoto", "Híbrido": "hibrido"}
            workplace = wp_map.get(remoto_val)

            # Filter out non-allowed onsite cities returned when querying "Brasil"
            if is_rem_q and workplace != "remoto":
                norm_loc = normalize_text(location or "")
                if not any(ac in norm_loc for ac in allowed_city_norms):
                    continue

            cand_url = str(item.get("urlCandidatura") or "").strip()
            detail_url = self.config.public_detail_urls_by_id.get(item_id) or (
                cand_url
                if re.fullmatch(r"https://www\.empregare\.com/pt-br/vaga-[\w-]+_\d+/?", cand_url)
                else None
            )
            url = detail_url or cand_url
            if not title or not url.startswith("https://"):
                continue

            description = f"{title}\nEmpresa: {company or 'Não informada'}\nLocal: {location or 'Não informado'}"
            incomplete = True
            published = item.get("dataPublicacao") or item.get("dataCadastro")
            if detail_url and not stop_details:
                try:
                    raw_html = await self.client.get_text(
                        detail_url, rate_limit_seconds=self.config.rate_limit_seconds
                    )
                    full_txt = html_to_clean_text(raw_html)
                    if "Descrição e Responsabilidades" in full_txt and item_id in full_txt:
                        description = full_txt.split("Descrição e Responsabilidades", 1)[1].split(
                            "Código da vaga:", 1
                        )[0].strip()
                        reg_m = re.search(r"Data de Cadastro:\s*(\d{4}-\d{2}-\d{2})", full_txt)
                        published = reg_m.group(1) if reg_m else published
                        incomplete = False
                except BudgetExhaustedError:
                    stop_details = True
                except AccessRestrictedError as exc:
                    errors.append({"url": detail_url, "error": str(exc), "status_code": exc.status_code})
                    if exc.status_code in {401, 403, 429}:
                        stop_details = True
                except ConnectorError as exc:
                    errors.append({"url": detail_url, "error": str(exc)})

            if published and not is_published_within_days(published, max_days=MAX_RECENCY_DAYS, allow_missing=False):
                continue

            pdf_ok = bool(
                re.search(
                    r"curr[ií]culo.{0,40}pdf|anex(?:e|ar).{0,25}curr[ií]culo|arquivo.{0,25}pdf",
                    normalize_text(description),
                )
            )
            jobs.append(
                RawJobInput(
                    source=self.source_key,
                    source_id=item_id,
                    title=title,
                    company=company,
                    url=url,
                    description=description,
                    location=location,
                    workplace_mode=workplace,  # type: ignore[arg-type]
                    country="BR",
                    published_at=normalize_published_iso(published) if published else None,
                    incomplete_summary=incomplete,
                    requires_login=True,
                    pdf_confirmed=pdf_ok,
                    source_method_hint="B" if pdf_ok else "C",
                    source_effort_hint="medio",
                )
            )

        return jobs, errors, {"queries_executed": len(queries), "extracted_jobs": len(jobs)}


def build_connector(
    config: JobSourceEntry,
    client: SafeAsyncHttpClient,
    criteria: SearchCriteria,
    *,
    gemini_api_key: str = "",
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> JobConnectorProtocol:
    if isinstance(config, (RssLocalSourceConfig, PublicPagesSourceConfig)):
        return RssAndPagesConnector(config, client, criteria)
    if isinstance(config, SitemapSourceConfig):
        return SitemapJsonLdConnector(config, client, criteria)
    if isinstance(config, (GreenhouseSourceConfig, LeverSourceConfig)):
        return GreenhouseAndLeverConnector(config, client, criteria)
    if isinstance(config, (AshbySourceConfig, RecruiteeSourceConfig)):
        return AshbyAndRecruiteeConnector(config, client, criteria)
    if isinstance(config, GupyApiSourceConfig):
        return GupyApiConnector(config, client, criteria)
    if isinstance(config, GitHubIssuesSourceConfig):
        return GitHubIssuesConnector(config, client, criteria)
    if isinstance(config, InhireSourceConfig):
        return InhireApiConnector(config, client, criteria)
    if isinstance(config, WorkableSourceConfig):
        return WorkableApiConnector(config, client, criteria)
    if isinstance(config, HtmlBoardSourceConfig):
        return HtmlJobBoardConnector(config, client, criteria)
    if isinstance(config, WebSearchSourceConfig):
        return WebSearchJobConnector(
            config,
            client,
            criteria,
            gemini_api_key=gemini_api_key,
            llm_transport=llm_transport,
        )
    if isinstance(config, EmpregareMcpSourceConfig):
        return EmpregareMcpConnector(config, client, criteria)
    if isinstance(config, PlaywrightPublicBoardConfig):
        from app.connectors.playwright_connector import PlaywrightPublicConnector

        return PlaywrightPublicConnector(config, client, criteria)
    raise ValueError(f"Unsupported job source config: {type(config).__name__}")
