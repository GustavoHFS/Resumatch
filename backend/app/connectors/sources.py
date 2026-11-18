"""Concrete deterministic job discovery connectors for configured public sources."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Protocol
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from app.connectors.http_client import (
    AccessRestrictedError,
    BudgetExhaustedError,
    ConnectorError,
    SafeAsyncHttpClient,
    extract_json_ld_job_posting,
    html_to_clean_text,
)
from app.domain.triage_engine import RawJobInput, contains_term, normalize_text
from app.models.criteria_schema import SearchCriteria
from app.models.sources_schema import (
    EmpregareMcpSourceConfig,
    GreenhouseSourceConfig,
    JobSourceEntry,
    LeverSourceConfig,
    PlaywrightPublicBoardConfig,
    PublicPagesSourceConfig,
    RssLocalSourceConfig,
    SitemapSourceConfig,
)


class JobConnectorProtocol(Protocol):
    source_key: str

    async def discover(
        self, seen_source_ids: dict[str, str] | None = None
    ) -> tuple[list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
        ...


def collect_allowed_hosts(sources: list[JobSourceEntry]) -> set[str]:
    hosts: set[str] = set()
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
        tree.css_first(".job-description")
        or tree.css_first(".section_content:not(.section_content_comments)")
        or tree.css_first(".general-content")
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
    if not title or len(description) < 20:
        raise ConnectorError("Public HTML page lacks recognizable title or job description.")

    company = seed.get("company")
    if not company and ld and isinstance(ld.get("hiringOrganization"), dict):
        company = html_to_clean_text(str(ld["hiringOrganization"].get("name") or "")) or None
    if not company:
        comp_node = tree.css_first(".job-company-name") or tree.css_first(".company")
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
        jloc = ld["jobLocation"][0] if isinstance(ld["jobLocation"], list) and ld["jobLocation"] else ld["jobLocation"]
        if isinstance(jloc, dict) and isinstance(jloc.get("address"), dict):
            addr = jloc["address"]
            city = addr.get("addressLocality")
            region = addr.get("addressRegion")
            location = " - ".join(x for x in [city, region] if x) or None
    if not location:
        loc_node = tree.css_first(".job-location")
        if loc_node:
            cand_loc = loc_node.text(strip=True)
            if normalize_text(cand_loc) not in {"sem localizacao", "nao informado", ""}:
                location = cand_loc

    published = seed.get("published_at") or (ld.get("datePosted") if ld else None)
    if not published:
        meta_pub = tree.css_first('meta[property="article:published_time"]')
        if meta_pub and meta_pub.attributes.get("content"):
            published = meta_pub.attributes["content"]

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
        published_at=published if isinstance(published, str) else None,
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
                        if not _match_catalog_areas(title, self.criteria) or _is_senior_title(
                            title, self.criteria
                        ):
                            continue
                        pub_raw = item.findtext("pubDate")
                        try:
                            pub_iso = parsedate_to_datetime(pub_raw).isoformat() if pub_raw else None
                        except (TypeError, ValueError):
                            pub_iso = None
                        candidates.setdefault(
                            link, {"url": link, "title": title, "published_at": pub_iso}
                        )
                except BudgetExhaustedError:
                    break
                except ConnectorError as exc:
                    errors.append({"url": feed_url, "error": str(exc)})

        # Round-robin across matched areas up to max_items
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
            try:
                raw_html = await self.client.get_text(
                    url, rate_limit_seconds=self.config.rate_limit_seconds
                )
                jobs.append(_extract_html_job_page(raw_html, url, self.source_key, seed))
            except BudgetExhaustedError:
                break
            except AccessRestrictedError as exc:
                errors.append({"url": url, "error": str(exc), "status_code": exc.status_code})
                # Preserve blocked lead as Tier 3 / Tier 2 instead of discarding!
                if seed.get("title"):
                    jobs.append(
                        RawJobInput(
                            source=self.source_key,
                            source_id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
                            title=seed["title"],
                            company=seed.get("company"),
                            url=url,
                            description=f"{seed['title']} - {seed.get('company') or ''}".strip(" -"),
                            location=seed.get("location"),
                            published_at=seed.get("published_at"),
                            incomplete_summary=True,
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
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.recency_days)

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
                        try:
                            dt = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            if dt < cutoff:
                                continue
                        except ValueError:
                            pass
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
                job = _extract_html_job_page(raw_html, url, self.source_key)
                job.source_id = item_id
                job.updated_at_source = lastmod or None
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
                published = raw.get("first_published")
                opp_type = "banco_talentos" if raw.get("internal_job_id", 1) is None else "vaga"
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
                created_ms = raw.get("createdAt")
                published = (
                    datetime.fromtimestamp(created_ms / 1000, timezone.utc).isoformat()
                    if isinstance(created_ms, (int, float))
                    else None
                )
                opp_type = "vaga"

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
                    published_at=published if isinstance(published, str) else None,
                    updated_at_source=raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
                    opportunity_type=opp_type,  # type: ignore[arg-type]
                    validity_status="confirmadamente_aberta",
                )
            )
            if len(jobs) >= self.config.max_items:
                break

        return jobs, errors, {"board_rows": len(rows), "extracted_jobs": len(jobs)}


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
            ["Brasil"]
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
                queries.append({"query": query_text, "city": city})
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
                    "clientInfo": {"name": "Resumatch-PublicDiscovery", "version": "0.1.0"},
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
        found_items: dict[str, dict[str, Any]] = {}

        for q in queries:
            try:
                call_res = await self.client.post_empregare_mcp_readonly(
                    self.config.endpoint,
                    "tools/call",
                    {
                        "name": "buscar_vagas",
                        "arguments": {
                            "query": q["query"],
                            "localidade": q["city"],
                            "pagina": 1,
                            "itensPagina": 5,
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
                        found_items[str(v["id"])] = v
            except BudgetExhaustedError:
                break
            except (ConnectorError, ValueError) as exc:
                errors.append({"query": q, "error": str(exc)})

        stop_details = False
        for item_id, item in list(found_items.items())[: self.config.max_items]:
            if item_id in seen:
                continue
            title = str(item.get("cargo") or "").strip()
            company = str(item.get("empresa") or "").strip() or None
            location = "; ".join(item.get("cidades") or []) or None
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
            published = None
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
                        published = reg_m.group(1) if reg_m else None
                        incomplete = False
                except BudgetExhaustedError:
                    stop_details = True
                except AccessRestrictedError as exc:
                    errors.append({"url": detail_url, "error": str(exc), "status_code": exc.status_code})
                    if exc.status_code in {401, 403, 429}:
                        stop_details = True
                except ConnectorError as exc:
                    errors.append({"url": detail_url, "error": str(exc)})

            wp_map = {"Presencial": "presencial", "Remoto": "remoto", "Híbrido": "hibrido"}
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
                    workplace_mode=wp_map.get(str(item.get("remoto") or "")),  # type: ignore[arg-type]
                    country="BR",
                    published_at=published,
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
) -> JobConnectorProtocol:
    if isinstance(config, (RssLocalSourceConfig, PublicPagesSourceConfig)):
        return RssAndPagesConnector(config, client, criteria)
    if isinstance(config, SitemapSourceConfig):
        return SitemapJsonLdConnector(config, client, criteria)
    if isinstance(config, (GreenhouseSourceConfig, LeverSourceConfig)):
        return GreenhouseAndLeverConnector(config, client, criteria)
    if isinstance(config, EmpregareMcpSourceConfig):
        return EmpregareMcpConnector(config, client, criteria)
    if isinstance(config, PlaywrightPublicBoardConfig):
        from app.connectors.playwright_connector import PlaywrightPublicConnector

        return PlaywrightPublicConnector(config, client, criteria)
    raise ValueError(f"Unsupported job source config: {type(config).__name__}")
