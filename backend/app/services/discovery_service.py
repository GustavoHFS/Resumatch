"""Discovery orchestration, canonical SQLite deduplication, and assisted manual import."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import Field
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.connectors.http_client import SafeAsyncHttpClient, validate_public_https_url
from app.connectors.sources import build_connector, collect_allowed_hosts
from app.core.config import AppSettings
from app.domain.triage_engine import (
    DeterministicTriageEngine,
    RawJobInput,
    TriageResult,
    canonicalize_url,
    compute_job_fingerprint,
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
    SearchCriteria,
)
from app.models.profile_schema import StrictModel


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssistedJobImportRequest(StrictModel):
    url: str = Field(min_length=8, max_length=1000)
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
            if opp:
                return opp
        opp_by_fp = session.exec(
            select(JobOpportunity).where(JobOpportunity.content_fingerprint == fingerprint)
        ).first()
        if opp_by_fp:
            return opp_by_fp

    # 3. Canonical URL match (verified against normalized title when multiple listings share a page)
    if canon_url:
        norm_title = normalize_text(title).strip()
        opp_candidates = session.exec(
            select(JobOpportunity).where(JobOpportunity.canonical_url == canon_url)
        ).all()
        for cand in opp_candidates:
            if normalize_text(cand.title).strip() == norm_title or len(opp_candidates) == 1:
                return cand

        obs_candidates = session.exec(
            select(JobObservation).where(JobObservation.canonical_url == canon_url)
        ).all()
        for obs in obs_candidates:
            opp = session.get(JobOpportunity, obs.opportunity_id)
            if opp and normalize_text(opp.title).strip() == norm_title:
                return opp

    return None


def upsert_and_triage_job(
    session: Session,
    triage_engine: DeterministicTriageEngine,
    raw_job: RawJobInput,
    now_iso: str | None = None,
    create_tracking_if_manual: bool = False,
) -> tuple[JobOpportunity, TriageResult, bool]:
    """Evaluates `raw_job` and upserts canonically without overwriting user-progressed stages."""
    timestamp = now_iso or utc_now_iso()
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

    existing = _find_existing_opportunity(
        session=session,
        source_name=raw_job.source,
        source_item_id=source_id,
        canon_url=canon_url,
        fingerprint=fp,
        title=raw_job.title,
    )

    raw_meta = {
        k: v
        for k, v in raw_job.model_dump(mode="json").items()
        if k != "description"
    }
    triage_dict = triage.model_dump(mode="json")
    reqs_json = json.dumps(triage_dict.pop("requirements", []), ensure_ascii=False)
    analysis_json = json.dumps(triage_dict, ensure_ascii=False)

    created = existing is None
    if existing is None:
        opp = JobOpportunity(
            id=str(uuid4()),
            discovered_at=timestamp,
            last_seen_at=timestamp,
            published_at=raw_job.published_at,
            company=raw_job.company,
            title=raw_job.title,
            original_url=raw_job.url,
            canonical_url=canon_url,
            content_fingerprint=fp,
            primary_source=raw_job.source,
            source_job_id=source_id,
            location=raw_job.location,
            workplace_mode=triage.inferred_workplace_mode,
            contract_type=triage.inferred_contract,
            opportunity_type=raw_job.opportunity_type,
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
        opp.last_seen_at = timestamp
        # Do not overwrite full description with an incomplete summary!
        if raw_job.description and (not raw_job.incomplete_summary or not opp.description_text):
            opp.description_text = raw_job.description
        opp.company = raw_job.company or opp.company
        opp.title = raw_job.title
        opp.published_at = raw_job.published_at or opp.published_at
        opp.location = raw_job.location or opp.location
        opp.workplace_mode = triage.inferred_workplace_mode or opp.workplace_mode
        opp.contract_type = triage.inferred_contract or opp.contract_type
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
        if fp and not opp.content_fingerprint:
            opp.content_fingerprint = fp
        # Preserve user-advanced application stages
        if opp.application_stage in {"descoberta", ""}:
            opp.application_stage = triage.initial_application_stage
        opp.requirements_json = reqs_json
        opp.analysis_json = analysis_json
        opp.raw_payload_json = json.dumps(raw_meta, ensure_ascii=False)
        session.add(opp)
        session.flush()

    # Upsert source sighting observation
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
    validate_public_https_url(payload.url)
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

    canon = canonicalize_url(payload.url)
    raw_job = RawJobInput(
        source="manual:assisted_import",
        source_id=hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24],
        title=inferred_title,
        company=payload.company,
        url=payload.url,
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


async def execute_discovery_run(
    db_engine: Engine,
    settings: AppSettings,
    *,
    max_requests_budget: int = 50,
    source_ids: list[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    default_rate_limit_seconds: float = 0.35,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    profile, criteria, blocked, sources_cfg = load_domain_configs(settings)
    triage_engine = DeterministicTriageEngine(criteria=criteria, blocked=blocked, profile=profile)

    selected_sources = [
        s
        for s in sources_cfg.sources
        if s.enabled and (source_ids is None or s.id in source_ids)
    ]
    # Order primary ('high') sources before 'secondary' sources
    selected_sources.sort(key=lambda s: s.priority == "secondary")

    with Session(db_engine) as session:
        seen_by_source = _load_seen_source_map(session)

    allowed_hosts = collect_allowed_hosts(selected_sources)
    discovered_jobs: list[RawJobInput] = []
    source_summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    secondary_used = 0

    async with SafeAsyncHttpClient(
        allowed_hosts=allowed_hosts,
        max_requests_budget=max_requests_budget,
        default_rate_limit_seconds=default_rate_limit_seconds,
        transport=transport,
    ) as http_client:
        for src_cfg in selected_sources:
            if src_cfg.priority == "secondary" and source_ids is None:
                if secondary_used >= criteria.career_focus.max_secondary_sources_per_run:
                    continue
                secondary_used += 1

            connector = build_connector(src_cfg, http_client, criteria)
            seen_for_connector = seen_by_source.get(connector.source_key, {})
            found, errs, stats = await connector.discover(seen_for_connector)
            discovered_jobs.extend(found)
            for err in errs:
                failures.append({"source_id": src_cfg.id, **err})
            source_summaries.append(
                {
                    "source_id": src_cfg.id,
                    "source_key": connector.source_key,
                    "found": len(found),
                    "errors": len(errs),
                    **stats,
                }
            )
            if http_client.requests_made >= max_requests_budget:
                break

        requests_made = http_client.requests_made

    new_count = 0
    unique_ids: set[str] = set()
    classifications: Counter[str] = Counter()
    tiers: Counter[int] = Counter()

    with Session(db_engine) as session:
        for raw_job in discovered_jobs:
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
        summary = {
            "id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "requests_made": requests_made,
            "max_requests_budget": max_requests_budget,
            "extracted_count": len(discovered_jobs),
            "unique_count": len(unique_ids),
            "new_count": new_count,
            "deduplicated_existing_count": len(discovered_jobs) - new_count,
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
                extracted_count=len(discovered_jobs),
                unique_count=len(unique_ids),
                new_count=new_count,
                summary_json=json.dumps(summary, ensure_ascii=False),
            )
        )
        session.commit()

    return summary
