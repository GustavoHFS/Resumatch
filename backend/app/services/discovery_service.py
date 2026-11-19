"""Discovery orchestration, concurrent execution (`Semaphore(6)`), automatic web-search fallback,
anti-ghost-job protection, canonical SQLite deduplication, and SQLite triage reconciliation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from pydantic import Field
from sqlalchemy import Engine
from sqlmodel import Session, col, delete, select

from app.connectors.anti_garbage import is_garbage_job_candidate
from app.connectors.http_client import SafeAsyncHttpClient, validate_public_https_url
from app.connectors.sources import build_connector, collect_allowed_hosts
from app.connectors.web_search import WebSearchJobConnector
from app.core.config import AppSettings
from app.domain.date_utils import (
    is_published_within_days,
    normalize_published_iso,
    parse_published_at_utc,
    to_canonical_utc_iso,
)
from app.domain.triage_engine import (
    DeterministicTriageEngine,
    RawJobInput,
    TriageResult,
    canonicalize_url,
    compute_cross_portal_signature,
    compute_job_fingerprint,
    is_manual_source,
    is_non_tech_role,
    normalize_text,
)
from app.models import (
    ApplicationEvent,
    ApplicationTracking,
    BlockedCompaniesConfig,
    DiscoveryRun,
    JobObservation,
    JobOpportunity,
    JobSourcesConfig,
    MasterProfile,
    ResumeVersion,
    SearchCriteria,
)
from app.models.profile_schema import StrictModel
from app.models.sources_schema import HtmlBoardSourceConfig, WebSearchSourceConfig


def utc_now_iso() -> str:
    return to_canonical_utc_iso(datetime.now(timezone.utc))


class AssistedJobImportRequest(StrictModel):
    url: str = Field(default="", max_length=1000)
    title: str | None = Field(default=None, max_length=300)
    company: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=100000)
    workplace_mode: Literal["presencial", "hibrido", "remoto"] | None = None
    contract_hint: str | None = Field(default=None, max_length=100)
    opportunity_type: Literal["vaga", "recrutador", "banco_talentos", "prospeccao"] = "vaga"
    pdf_confirmed: bool = False
    access_blocked: bool = False
    requires_login: bool = False


class _PerSourceClientView:
    """Enforces a fair per-source request cap on top of the shared `SafeAsyncHttpClient`."""

    def __init__(self, inner: SafeAsyncHttpClient, per_source_cap: int) -> None:
        self._inner = inner
        self._cap = max(2, per_source_cap)
        self.local_requests = 0

    def _guard_local(self) -> None:
        from app.connectors.http_client import BudgetExhaustedError

        if self.local_requests >= self._cap:
            raise BudgetExhaustedError(f"Per-source request cap reached ({self._cap}).")
        self.local_requests += 1

    async def get_text(self, url: str, **kwargs: Any) -> str:
        self._guard_local()
        return await self._inner.get_text(url, **kwargs)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        self._guard_local()
        return await self._inner.get_json(url, **kwargs)

    async def post_empregare_mcp_readonly(
        self, endpoint: str, method: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        self._guard_local()
        return await self._inner.post_empregare_mcp_readonly(endpoint, method, params, **kwargs)


def load_domain_configs(
    settings: AppSettings,
) -> tuple[MasterProfile, SearchCriteria, BlockedCompaniesConfig, JobSourcesConfig]:
    profile = MasterProfile.model_validate(
        json.loads(settings.profile_path.read_text(encoding="utf-8"))
    )
    criteria = SearchCriteria.model_validate(
        json.loads(settings.criteria_path.read_text(encoding="utf-8"))
    )
    blocked = BlockedCompaniesConfig.model_validate(
        json.loads(settings.blocked_companies_path.read_text(encoding="utf-8"))
    )
    sources = JobSourcesConfig.model_validate(
        json.loads(settings.sources_path.read_text(encoding="utf-8"))
    )
    return profile, criteria, blocked, sources


def _find_existing_opportunity(
    session: Session,
    source_name: str,
    source_item_id: str,
    canon_url: str,
    fingerprint: str | None,
    xportal_sig: str | None,
    title: str,
) -> JobOpportunity | None:
    # 1. Exact (source_name, source_item_id) match in JobObservation
    obs_by_src = session.exec(
        select(JobObservation).where(
            JobObservation.observation_kind == "source_sighting",
            JobObservation.source_name == source_name,
            JobObservation.source_item_id == source_item_id,
        )
    ).first()
    if obs_by_src:
        opp = session.get(JobOpportunity, obs_by_src.opportunity_id)
        if opp:
            return opp

    # Manual listings cannot overwrite another job just because they share a
    # careers page, title or cross-portal signature.
    if is_manual_source(source_name):
        if fingerprint:
            exact = session.exec(
                select(JobOpportunity).where(JobOpportunity.content_fingerprint == fingerprint)
            ).first()
            if exact and not is_manual_source(exact.primary_source):
                return exact
        return None

    # 2. Content fingerprint match (identical company + title + location + full description)
    if fingerprint:
        obs_by_fp = session.exec(
            select(JobObservation).where(
                JobObservation.observation_kind == "source_sighting",
                JobObservation.content_fingerprint == fingerprint,
            )
        ).first()
        if obs_by_fp:
            opp = session.get(JobOpportunity, obs_by_fp.opportunity_id)
            if opp and not is_manual_source(opp.primary_source):
                return opp
        opp_by_fp = session.exec(
            select(JobOpportunity).where(JobOpportunity.content_fingerprint == fingerprint)
        ).first()
        if opp_by_fp and not is_manual_source(opp_by_fp.primary_source):
            return opp_by_fp

    # 3. Canonical URL match (verified against normalized title when multiple listings share a page)
    if canon_url:
        norm_title = normalize_text(title).strip()
        opp_candidates = session.exec(
            select(JobOpportunity).where(JobOpportunity.canonical_url == canon_url)
        ).all()
        for cand in opp_candidates:
            if not is_manual_source(cand.primary_source) and (
                normalize_text(cand.title).strip() == norm_title or len(opp_candidates) == 1
            ):
                return cand

        obs_candidates = session.exec(
            select(JobObservation).where(JobObservation.canonical_url == canon_url)
        ).all()
        for obs in obs_candidates:
            opp = session.get(JobOpportunity, obs.opportunity_id)
            if opp and not is_manual_source(opp.primary_source) and normalize_text(opp.title).strip() == norm_title:
                return opp

    # 4. Cross-portal normalized signature (`slug(company)__slug(clean_title)__slug(city_or_remoto)`)
    if xportal_sig:
        opp_by_xsig = session.exec(
            select(JobOpportunity).where(JobOpportunity.content_fingerprint == xportal_sig)
        ).first()
        if opp_by_xsig and not is_manual_source(opp_by_xsig.primary_source):
            return opp_by_xsig

        company_rows = session.exec(
            select(JobOpportunity).where(JobOpportunity.company.is_not(None))  # type: ignore[union-attr]
        ).all()
        for cand in company_rows:
            cand_sig = compute_cross_portal_signature(
                cand.company,
                cand.title,
                cand.location,
                cand.workplace_mode,
            )
            if cand_sig == xportal_sig and not is_manual_source(cand.primary_source):
                return cand

    return None


def _validate_manual_job_url(url: str) -> None:
    """Manual URLs are never fetched: keep public-host checks, allow ATS tokens.

    The generated local marker is accepted only for manual jobs and is not a
    connector allowlist entry. Other localhost/private URLs remain forbidden.
    """
    parsed = urlsplit(url)
    if parsed.hostname == "manual.resumatch.local" and parsed.path.startswith("/vaga/"):
        if parsed.scheme == "https" and not parsed.username and not parsed.password and not parsed.port:
            return
    validate_public_https_url(urlunsplit(parsed._replace(query="", fragment="")))


def _is_ghost_repost(session: Session, opp: JobOpportunity, now_iso: str) -> bool:
    """Returns True if this opportunity was already first discovered > 30 days ago."""
    now_dt = parse_published_at_utc(now_iso) or datetime.now(timezone.utc)
    candidates_dt: list[datetime] = []
    if opp.discovered_at:
        d0 = parse_published_at_utc(opp.discovered_at)
        if d0:
            candidates_dt.append(d0)
    if opp.published_at:
        p0 = parse_published_at_utc(opp.published_at)
        if p0:
            candidates_dt.append(p0)
    obs_dates = session.exec(
        select(JobObservation.observed_at).where(JobObservation.opportunity_id == opp.id)
    ).all()
    for raw_obs in obs_dates:
        dt_obs = parse_published_at_utc(raw_obs)
        if dt_obs:
            candidates_dt.append(dt_obs)
    if not candidates_dt:
        return False
    earliest = min(candidates_dt)
    return (now_dt - earliest).days > 30


def upsert_and_triage_job(
    session: Session,
    triage_engine: DeterministicTriageEngine,
    raw_job: RawJobInput,
    now_iso: str | None = None,
    create_tracking_if_manual: bool = False,
) -> tuple[JobOpportunity, TriageResult, bool]:
    """Evaluates `raw_job` and upserts canonically with honest dates & anti-ghost-job protection."""
    timestamp = now_iso or utc_now_iso()
    if is_manual_source(raw_job.source):
        _validate_manual_job_url(raw_job.url)
    else:
        validate_public_https_url(raw_job.url)
    canon_url = canonicalize_url(raw_job.url)
    fp = compute_job_fingerprint(
        raw_job.company,
        raw_job.title,
        raw_job.location,
        raw_job.description if not raw_job.incomplete_summary else None,
    )
    source_id = raw_job.source_id or hashlib.sha256(canon_url.encode("utf-8")).hexdigest()[:24]
    triage = triage_engine.evaluate(raw_job)

    is_manual = raw_job.source.startswith("manual:")
    published_iso = normalize_published_iso(
        raw_job.published_at,
        fallback_iso=timestamp if is_manual else None,
    )
    xportal_sig = compute_cross_portal_signature(
        raw_job.company,
        raw_job.title,
        raw_job.location,
        triage.inferred_workplace_mode or raw_job.workplace_mode,
    )
    effective_fp = fp or xportal_sig

    existing = _find_existing_opportunity(
        session=session,
        source_name=raw_job.source,
        source_item_id=source_id,
        canon_url=canon_url,
        fingerprint=fp,
        xportal_sig=xportal_sig,
        title=raw_job.title,
    )

    opp_type = raw_job.opportunity_type
    if opp_type == "vaga" and any(
        tok in normalize_text(raw_job.title) for tok in ("banco de talentos", "talent pool")
    ):
        opp_type = "banco_talentos"

    raw_meta = {
        k: v
        for k, v in raw_job.model_dump(mode="json").items()
        if k != "description"
    }
    triage_dict = triage.model_dump(mode="json")

    created = existing is None
    if existing is None:
        reqs_json = json.dumps(triage_dict.pop("requirements", []), ensure_ascii=False)
        analysis_json = json.dumps(triage_dict, ensure_ascii=False)
        opp = JobOpportunity(
            id=str(uuid4()),
            discovered_at=timestamp,
            last_seen_at=timestamp,
            published_at=published_iso,
            company=raw_job.company,
            title=raw_job.title,
            original_url=raw_job.url,
            canonical_url=canon_url,
            content_fingerprint=effective_fp,
            primary_source=raw_job.source,
            source_job_id=source_id,
            location=raw_job.location,
            workplace_mode=triage.inferred_workplace_mode,
            contract_type=triage.inferred_contract,
            opportunity_type=opp_type,
            area_id=triage.area_id,
            career_tier=triage.career_tier,
            classification=triage.classification,
            professional_compatibility=triage.professional_compatibility,
            operational_status=triage.operational_status,
            relevance_score=triage.relevance_score,
            practical_priority=triage.practical_priority,
            validity_status=raw_job.validity_status,
            application_method=triage.application_method,
            application_effort=triage.application_effort,
            automation_capability=triage.automation_capability,
            service_tier=triage.service_tier,
            application_stage=triage.initial_application_stage,
            description_text=raw_job.description,
            requirements_json=reqs_json,
            analysis_json=analysis_json,
            raw_payload_json=json.dumps(raw_meta, ensure_ascii=False),
        )
        session.add(opp)
        session.flush()
    else:
        opp = existing
        if "gupy" in raw_job.source.lower() or (urlsplit(raw_job.url).hostname or "").endswith(".gupy.io"):
            opp.primary_source = raw_job.source
            opp.original_url = raw_job.url
            opp.canonical_url = canon_url
        is_ghost = _is_ghost_repost(session, opp, timestamp)
        if is_ghost:
            triage_dict["ghost_repost_detected"] = True
        reqs_json = json.dumps(triage_dict.pop("requirements", []), ensure_ascii=False)
        analysis_json = json.dumps(triage_dict, ensure_ascii=False)

        opp.last_seen_at = timestamp
        if raw_job.description and (not raw_job.incomplete_summary or not opp.description_text):
            opp.description_text = raw_job.description
        opp.company = raw_job.company or opp.company
        opp.title = raw_job.title
        # Anti-ghost-job rule: never overwrite published_at of an old (>30d) job with a fresh repost date!
        if published_iso and not is_ghost:
            if not opp.published_at or published_iso < opp.published_at:
                # Preserve earliest real publication date when seen across portals
                opp.published_at = published_iso
        opp.location = raw_job.location or opp.location
        opp.workplace_mode = triage.inferred_workplace_mode or opp.workplace_mode
        opp.contract_type = triage.inferred_contract or opp.contract_type
        opp.opportunity_type = opp_type
        opp.area_id = triage.area_id or opp.area_id
        opp.career_tier = triage.career_tier
        opp.classification = triage.classification
        opp.professional_compatibility = triage.professional_compatibility
        opp.operational_status = triage.operational_status
        opp.relevance_score = triage.relevance_score
        opp.practical_priority = triage.practical_priority
        opp.validity_status = raw_job.validity_status
        opp.application_method = triage.application_method
        opp.application_effort = triage.application_effort
        opp.automation_capability = triage.automation_capability
        opp.service_tier = triage.service_tier
        if effective_fp and not opp.content_fingerprint:
            opp.content_fingerprint = effective_fp
        if opp.application_stage in {"descoberta", ""}:
            opp.application_stage = triage.initial_application_stage
        opp.requirements_json = reqs_json
        opp.analysis_json = analysis_json
        opp.raw_payload_json = json.dumps(raw_meta, ensure_ascii=False)
        session.add(opp)
        session.flush()

    existing_obs = session.exec(
        select(JobObservation).where(
            JobObservation.observation_kind == "source_sighting",
            JobObservation.source_name == raw_job.source,
            JobObservation.source_item_id == source_id,
        )
    ).first()
    if existing_obs:
        existing_obs.canonical_url = canon_url
        existing_obs.content_fingerprint = fp
        existing_obs.observed_at = timestamp
        existing_obs.payload_json = json.dumps(raw_meta, ensure_ascii=False)
        session.add(existing_obs)
    else:
        session.add(
            JobObservation(
                opportunity_id=opp.id,
                observation_kind="source_sighting",
                source_name=raw_job.source,
                source_item_id=source_id,
                canonical_url=canon_url,
                content_fingerprint=fp,
                observed_at=timestamp,
                payload_json=json.dumps(raw_meta, ensure_ascii=False),
            )
        )

    if create_tracking_if_manual:
        tracking = session.get(ApplicationTracking, opp.id)
        if not tracking:
            session.add(
                ApplicationTracking(
                    opportunity_id=opp.id,
                    updated_at=timestamp,
                    stage=opp.application_stage,
                    queue_state=(
                        "candidatura_a_preparar"
                        if triage.local_prep_allowed
                        else "necessita_intervencao_usuario"
                        if triage.service_tier == 3
                        else "aguardando_confirmacao"
                    ),
                    professional_compatibility=triage.professional_compatibility,
                    operational_status=triage.operational_status,
                    service_tier=triage.service_tier,
                    automation_capability=triage.automation_capability,
                    application_method=triage.application_method,
                    pdf_status=triage.pdf_status,
                    submission_instructions=" | ".join(triage.submission_instructions),
                    notes="Imported via Assisted Job Import",
                )
            )
            session.add(
                ApplicationEvent(
                    opportunity_id=opp.id,
                    created_at=timestamp,
                    event_source="tracking",
                    stage=opp.application_stage,
                    notes="Assisted manual import evaluated deterministically",
                )
            )

    return opp, triage, created


def import_assisted_job(
    session: Session,
    settings: AppSettings,
    payload: AssistedJobImportRequest,
) -> dict[str, Any]:
    job_url = payload.url.strip() or f"https://manual.resumatch.local/vaga/{uuid4()}"
    _validate_manual_job_url(job_url)
    profile, criteria, blocked, _ = load_domain_configs(settings)
    engine = DeterministicTriageEngine(criteria=criteria, blocked=blocked, profile=profile)

    desc = payload.description.strip()
    inferred_title = payload.title
    if not inferred_title:
        first_line = next((ln.strip() for ln in desc.splitlines() if ln.strip()), "")
        inferred_title = first_line[:180] if first_line else "Oportunidade Importada"

    incomplete = len(desc) < 80
    if not desc:
        desc = f"{inferred_title} - {payload.company or 'Empresa não informada'}"

    canon = canonicalize_url(job_url)
    raw_job = RawJobInput(
        source="manual:assisted_import",
        source_id=hashlib.sha256(
            f"{canon}\n{inferred_title}\n{desc}".encode("utf-8")
        ).hexdigest()[:24],
        title=inferred_title,
        company=payload.company,
        url=job_url,
        description=desc,
        location=payload.location,
        workplace_mode=payload.workplace_mode,
        contract_hint=payload.contract_hint,
        opportunity_type=payload.opportunity_type,
        pdf_confirmed=payload.pdf_confirmed,
        incomplete_summary=incomplete,
        access_blocked=payload.access_blocked,
        requires_login=payload.requires_login,
    )

    opp, triage, created = upsert_and_triage_job(
        session=session,
        triage_engine=engine,
        raw_job=raw_job,
        create_tracking_if_manual=True,
    )
    session.commit()
    return {
        "opportunity_id": opp.id,
        "created": created,
        "deduplicated": not created,
        "canonical_url": opp.canonical_url,
        "application_stage": opp.application_stage,
        "triage": triage.model_dump(mode="json"),
    }


def _load_seen_source_map(session: Session) -> dict[str, dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    rows = session.exec(
        select(JobObservation).where(JobObservation.observation_kind == "source_sighting")
    ).all()
    for obs in rows:
        if not obs.source_item_id:
            continue
        updated_hint = ""
        try:
            meta = json.loads(obs.payload_json or "{}")
            updated_hint = str(meta.get("updated_at_source") or meta.get("atualizada_fonte_em") or "")
        except ValueError:
            pass
        seen.setdefault(obs.source_name, {})[obs.source_item_id] = updated_hint
    return seen


REMOTE_ONLY_URL_MARKERS = (
    "home-office",
    "home_office",
    "remoto",
    "remote",
    "workplaceType[]=remote".lower(),
)


def _is_remote_only_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in REMOTE_ONLY_URL_MARKERS)


def _source_without_remote_preference(src_cfg: Any) -> Any:
    """Returns a source copy with remote-only URLs/locations removed, or None if nothing remains."""
    if isinstance(src_cfg, WebSearchSourceConfig):
        locations = [
            loc
            for loc in src_cfg.locations
            if "remot" not in normalize_text(loc)
            and "home office" not in normalize_text(loc)
        ]
        return src_cfg.model_copy(update={"locations": locations or ["Brasil"]})
    if isinstance(src_cfg, HtmlBoardSourceConfig):
        urls = [url for url in src_cfg.search_urls if not _is_remote_only_url(url)]
        if not urls:
            return None
        return src_cfg.model_copy(update={"search_urls": urls})
    return src_cfg


async def execute_discovery_run(
    db_engine: Engine,
    settings: AppSettings,
    *,
    max_requests_budget: int = 140,
    source_ids: list[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    default_rate_limit_seconds: float = 0.35,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    profile, criteria, blocked, sources_cfg = load_domain_configs(settings)
    triage_engine = DeterministicTriageEngine(criteria=criteria, blocked=blocked, profile=profile)

    raw_selected = [
        s
        for s in sources_cfg.sources
        if s.enabled and (source_ids is None or s.id in source_ids)
    ]
    raw_selected.sort(key=lambda s: s.priority == "secondary")

    selected_sources = []
    secondary_used = 0
    for src_cfg in raw_selected:
        if not criteria.include_remote:
            restricted = _source_without_remote_preference(src_cfg)
            if restricted is None:
                continue
            src_cfg = restricted
        if src_cfg.priority == "secondary" and source_ids is None:
            if secondary_used >= criteria.career_focus.max_secondary_sources_per_run:
                continue
            secondary_used += 1
        selected_sources.append(src_cfg)

    with Session(db_engine) as session:
        seen_by_source = _load_seen_source_map(session)

    allowed_hosts = collect_allowed_hosts(selected_sources)
    # Guarantee enough global budget so regional Pernambuco sources are never starved by earlier portals
    effective_global_budget = (
        max(max_requests_budget, len(selected_sources) * 6)
        if len(selected_sources) > 6 and transport is None
        else max_requests_budget
    )
    per_source_cap = max(4, min(18, max_requests_budget))
    gemini_key = settings.gemini_api_key.strip() if transport is None or llm_transport is not None else ""

    discovered_jobs: list[RawJobInput] = []
    source_summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(6)

    async with SafeAsyncHttpClient(
        allowed_hosts=allowed_hosts,
        max_requests_budget=effective_global_budget,
        default_rate_limit_seconds=default_rate_limit_seconds,
        transport=transport,
    ) as http_client:

        async def _run_single_source(idx: int, src_cfg: Any) -> tuple[int, list[RawJobInput], list[dict[str, Any]], dict[str, Any]]:
            async with sem:
                scoped_client = _PerSourceClientView(http_client, per_source_cap)
                connector = build_connector(
                    src_cfg,
                    scoped_client,  # type: ignore[arg-type]
                    criteria,
                    gemini_api_key=gemini_key,
                    llm_transport=llm_transport,
                )
                seen_for_connector = seen_by_source.get(connector.source_key, {})
                found, errs, stats = await connector.discover(seen_for_connector)

                used_ws_fallback = False
                # Automatic WebSearchJobConnector fallback when an HTML/SPA portal returns 0 jobs or 403/5xx
                if (
                    not found
                    and isinstance(src_cfg, HtmlBoardSourceConfig)
                    and src_cfg.web_search_fallback
                    and (errs or stats.get("cards_found", 0) == 0)
                ):
                    primary_host = (
                        (urlsplit(src_cfg.search_urls[0]).hostname or "").lower().removeprefix("www.")
                    )
                    if primary_host and primary_host not in {"linkedin.com"}:
                        fb_cfg = WebSearchSourceConfig(
                            id=f"{src_cfg.id}_ws",
                            display_name=src_cfg.display_name,
                            category=src_cfg.category,
                            type="web_search",
                            target_domains=[primary_host],
                            company_fallback=src_cfg.company_fallback,
                            locations=(
                                [src_cfg.location_fallback]
                                if src_cfg.location_fallback
                                else ["Brasil", "Remoto"]
                            ),
                            requires_login_for_apply=src_cfg.requires_login_for_apply,
                            max_items=min(8, src_cfg.max_items),
                            rate_limit_seconds=src_cfg.rate_limit_seconds,
                        )
                        fb_conn = WebSearchJobConnector(
                            fb_cfg,
                            scoped_client,  # type: ignore[arg-type]
                            criteria,
                            gemini_api_key=gemini_key,
                            llm_transport=llm_transport,
                        )
                        fb_seen = seen_by_source.get(fb_conn.source_key, {})
                        fb_found, fb_errs, fb_stats = await fb_conn.discover(fb_seen)
                        if fb_found:
                            found = fb_found
                            used_ws_fallback = True
                            stats["web_search_fallback_jobs"] = len(fb_found)
                        errs.extend(fb_errs)

                summary_entry = {
                    "source_id": src_cfg.id,
                    "source_key": connector.source_key,
                    "found": len(found),
                    "errors": len(errs),
                    "used_web_search_fallback": used_ws_fallback,
                    **stats,
                }
                tagged_errs = [{"source_id": src_cfg.id, **e} for e in errs]
                return idx, found, tagged_errs, summary_entry

        results = await asyncio.gather(
            *(_run_single_source(i, sc) for i, sc in enumerate(selected_sources))
        )
        results.sort(key=lambda r: r[0])
        for _, found, tagged_errs, summary_entry in results:
            discovered_jobs.extend(found)
            failures.extend(tagged_errs)
            source_summaries.append(summary_entry)

        requests_made = http_client.requests_made

    new_count = 0
    discarded_old_count = 0
    discarded_garbage_count = 0
    discarded_non_tech_count = 0
    discarded_remote_count = 0
    unique_ids: set[str] = set()
    classifications: Counter[str] = Counter()
    tiers: Counter[int] = Counter()

    with Session(db_engine) as session:
        for raw_job in discovered_jobs:
            if is_garbage_job_candidate(raw_job.title, raw_job.url):
                discarded_garbage_count += 1
                continue
            if not is_published_within_days(raw_job.published_at, max_days=14):
                discarded_old_count += 1
                continue
            if not is_manual_source(raw_job.source) and is_non_tech_role(raw_job.title):
                discarded_non_tech_count += 1
                continue
            if not criteria.include_remote:
                remote_probe = triage_engine.evaluate(raw_job)
                if remote_probe.inferred_workplace_mode == "remoto":
                    discarded_remote_count += 1
                    continue
            opp, triage, created = upsert_and_triage_job(
                session=session,
                triage_engine=triage_engine,
                raw_job=raw_job,
                now_iso=started_at,
            )
            if opp.id not in unique_ids:
                unique_ids.add(opp.id)
                classifications[triage.classification] += 1
                tiers[triage.service_tier] += 1
            if created:
                new_count += 1

        finished_at = utc_now_iso()
        run_id = str(uuid4())
        recent_extracted = (
            len(discovered_jobs)
            - discarded_old_count
            - discarded_garbage_count
            - discarded_non_tech_count
            - discarded_remote_count
        )
        summary = {
            "id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "requests_made": requests_made,
            "max_requests_budget": effective_global_budget,
            "extracted_count": recent_extracted,
            "discarded_older_than_14d": discarded_old_count,
            "discarded_garbage_count": discarded_garbage_count,
            "discarded_non_tech_count": discarded_non_tech_count,
            "discarded_remote_count": discarded_remote_count,
            "unique_count": len(unique_ids),
            "new_count": new_count,
            "deduplicated_existing_count": max(0, recent_extracted - new_count),
            "classifications": dict(classifications),
            "service_tiers": {str(k): v for k, v in tiers.items()},
            "sources": source_summaries,
            "failures": failures,
        }
        session.add(
            DiscoveryRun(
                id=run_id,
                run_kind="discovery",
                started_at=started_at,
                finished_at=finished_at,
                extracted_count=recent_extracted,
                unique_count=len(unique_ids),
                new_count=new_count,
                summary_json=json.dumps(summary, ensure_ascii=False),
            )
        )
        session.commit()

    return summary


def reconcile_triage_in_sqlite(
    db_engine: Engine,
    settings: AppSettings,
    *,
    create_backup: bool = True,
    fast: bool = False,
) -> dict[str, Any]:
    """Purges navigation garbage rows and recalculates deterministic triage for all legacy SQLite opportunities."""
    backup_filename: str | None = None
    if create_backup and not fast and settings.db_path.is_file():
        settings.backups_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = settings.backups_dir / f"app.{ts}.bak.sqlite3"
        shutil.copy2(settings.db_path, backup_path)
        backup_filename = backup_path.name

    profile, criteria, blocked, _ = load_domain_configs(settings)
    triage_engine = DeterministicTriageEngine(criteria=criteria, blocked=blocked, profile=profile)

    purged_garbage = 0
    purged_non_tech = 0
    recalculated = 0
    previously_null_compat = 0
    classifications: Counter[str] = Counter()
    compatibilities: Counter[str] = Counter()

    with Session(db_engine) as session:
        all_opps = session.exec(select(JobOpportunity)).all()
        for opp in all_opps:
            if fast and is_manual_source(opp.primary_source):
                continue
            if fast:
                tracking = None
            else:
                tracking = session.get(ApplicationTracking, opp.id)
            is_user_progressed = tracking is not None and tracking.stage not in {
                "descoberta",
                "candidatura_a_preparar",
            }
            should_purge = not fast and not is_user_progressed and (
                is_garbage_job_candidate(opp.title, opp.original_url)
                or (
                    not is_manual_source(opp.primary_source)
                    and is_non_tech_role(opp.title)
                )
            )
            if should_purge:
                session.exec(delete(JobObservation).where(JobObservation.opportunity_id == opp.id))
                session.exec(
                    delete(ApplicationEvent).where(ApplicationEvent.opportunity_id == opp.id)
                )
                session.exec(
                    delete(ResumeVersion).where(ResumeVersion.opportunity_id == opp.id)
                )
                if tracking is not None:
                    session.delete(tracking)
                session.delete(opp)
                if is_non_tech_role(opp.title) and not is_manual_source(opp.primary_source):
                    purged_non_tech += 1
                else:
                    purged_garbage += 1
                continue

            if opp.professional_compatibility is None:
                previously_null_compat += 1

            raw_meta: dict[str, Any] = {}
            try:
                raw_meta = json.loads(opp.raw_payload_json or "{}")
            except ValueError:
                pass

            raw_input = RawJobInput(
                source=opp.primary_source,
                source_id=opp.source_job_id,
                title=opp.title,
                company=opp.company,
                url=opp.original_url,
                description=opp.description_text or opp.title,
                location=opp.location,
                workplace_mode=opp.workplace_mode,  # type: ignore[arg-type]
                contract_hint=opp.contract_type,
                published_at=opp.published_at,
                opportunity_type=opp.opportunity_type,  # type: ignore[arg-type]
                validity_status=opp.validity_status,  # type: ignore[arg-type]
                pdf_confirmed=bool(raw_meta.get("pdf_confirmed", False)),
                incomplete_summary=len(opp.description_text or "") < 80,
                access_blocked=bool(raw_meta.get("access_blocked", False)),
                requires_login=bool(raw_meta.get("requires_login", False)),
            )
            triage = triage_engine.evaluate(raw_input)
            triage_dict = triage.model_dump(mode="json")
            reqs_json = json.dumps(triage_dict.pop("requirements", []), ensure_ascii=False)
            analysis_json = json.dumps(triage_dict, ensure_ascii=False)

            opp.area_id = triage.area_id
            opp.career_tier = triage.career_tier
            opp.classification = triage.classification
            opp.professional_compatibility = triage.professional_compatibility
            opp.operational_status = triage.operational_status
            opp.relevance_score = triage.relevance_score
            opp.practical_priority = triage.practical_priority
            opp.workplace_mode = triage.inferred_workplace_mode or opp.workplace_mode
            opp.contract_type = triage.inferred_contract or opp.contract_type
            opp.application_method = triage.application_method
            opp.application_effort = triage.application_effort
            opp.automation_capability = triage.automation_capability
            opp.service_tier = triage.service_tier
            opp.requirements_json = reqs_json
            opp.analysis_json = analysis_json
            if opp.application_stage in {"descoberta", ""}:
                opp.application_stage = triage.initial_application_stage
            session.add(opp)

            recalculated += 1
            classifications[triage.classification] += 1
            compatibilities[triage.professional_compatibility] += 1

        session.commit()

    return {
        "backup_snapshot": backup_filename,
        "purged_garbage_count": purged_garbage,
        "purged_non_tech_count": purged_non_tech,
        "recalculated_count": recalculated,
        "previously_null_compatibility_count": previously_null_compat,
        "classifications": dict(classifications),
        "compatibilities": dict(compatibilities),
    }
