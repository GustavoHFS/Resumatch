"""Phase 2 REST endpoints for jobs, deterministic triage, discovery, sources, and scheduler."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import func
from sqlmodel import Session, col, or_, select

from app.api.routes import get_app_settings
from app.connectors.http_client import ConnectorError
from app.core.config import AppSettings
from datetime import datetime, timedelta, timezone

from app.core.database import get_db_session
from app.domain.date_utils import format_published_ago_pt, to_canonical_utc_iso
from app.domain.labels_pt import build_job_human_labels
from app.domain.triage_engine import (
    DeterministicTriageEngine,
    RawJobInput,
    TriageResult,
    is_manual_source,
)
from app.models import (
    ApplicationTracking,
    DiscoveryRun,
    JobObservation,
    JobOpportunity,
    JobSourcesConfig,
)
from app.models.profile_schema import StrictModel
from app.services.config_service import atomic_write_validated_json
from app.services.discovery_service import (
    AssistedJobImportRequest,
    execute_discovery_run,
    import_assisted_job,
    load_domain_configs,
)
from app.services.scheduler_service import DiscoverySchedulerManager

router = APIRouter(prefix="/api", tags=["discovery-and-triage"])


class DiscoveryRunRequest(StrictModel):
    max_requests_budget: int = Field(default=140, ge=1, le=300)
    source_ids: list[str] | None = None


class SourceToggleRequest(StrictModel):
    enabled: bool


class SchedulerConfigRequest(StrictModel):
    enabled: bool
    interval_minutes: int | None = Field(default=None, ge=15, le=10080)
    max_requests_budget: int | None = Field(default=None, ge=5, le=150)


def get_scheduler_manager(request: Request) -> DiscoverySchedulerManager:
    return request.app.state.scheduler


SOURCE_LABEL_MARKERS: tuple[tuple[str, str], ...] = (
    ("gupy", "Gupy"),
    ("linkedin", "LinkedIn"),
    ("github", "GitHub Vagas"),
    ("inhire", "Inhire"),
    ("empregospernambuco", "Empregos PE"),
    ("empregos_pe", "Empregos PE"),
    ("empregos-pernambuco", "Empregos PE"),
    ("empregare", "Empregare"),
    ("greenhouse", "Greenhouse"),
    ("lever", "Lever"),
    ("ashby", "Ashby"),
    ("recruitee", "Recruitee"),
    ("workable", "Workable"),
    ("remotar", "Remotar"),
    ("trampos", "Trampos"),
    ("programathor", "Programathor"),
    ("geekhunter", "GeekHunter"),
    ("coodesh", "Coodesh"),
    ("catho", "Catho"),
    ("infojobs", "InfoJobs"),
    ("vagas.com", "Vagas.com.br"),
    ("vagas_com", "Vagas.com.br"),
    ("bne", "BNE"),
    ("solides", "Sólides"),
    ("indeed", "Indeed"),
    ("glassdoor", "Glassdoor"),
    ("trabalha_brasil", "Trabalha Brasil"),
    ("trabalhabrasil", "Trabalha Brasil"),
    ("divulgavagas", "DivulgaVagas"),
    ("jooble", "Jooble"),
    ("jobvite", "Liferay"),
    ("carreira_publica", "Portal da empresa"),
    ("paginas_publicas", "Portal da empresa"),
    ("rss_local", "Empregos PE"),
    ("web_search", "Portal externo"),
)


def build_source_label(primary_source: str | None, url: str | None = None) -> str:
    """Friendly Portuguese label for the job's original source (never snake_case)."""
    source = (primary_source or "").strip()
    if is_manual_source(source):
        return "Adicionada manualmente"
    blob = f"{source} {url or ''}".lower()
    for marker, label in SOURCE_LABEL_MARKERS:
        if marker in blob:
            return label
    if ":" in source:
        tail = source.split(":", 1)[1].replace("_", " ").replace("-", " ").strip()
        if tail:
            return tail[:1].upper() + tail[1:]
    return "Portal externo"


@router.get("/jobs")
def list_jobs(
    area_id: str | None = None,
    classification: str | None = None,
    compatibility: str | None = None,
    stage: str | None = None,
    service_tier: int | None = Query(default=None, ge=1, le=3),
    method: str | None = None,
    recency: str | None = None,
    include_remote: bool | None = None,
    search: str | None = None,
    show_discarded: bool = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    filters = []
    if not show_discarded:
        filters.append(col(JobOpportunity.classification).notin_(("descartada", "fora_foco")))
    if area_id:
        filters.append(JobOpportunity.area_id == area_id)
    if classification:
        filters.append(JobOpportunity.classification == classification)
    if compatibility:
        filters.append(JobOpportunity.professional_compatibility == compatibility)
    if stage:
        filters.append(JobOpportunity.application_stage == stage)
    if service_tier is not None:
        filters.append(JobOpportunity.service_tier == service_tier)
    if method:
        filters.append(JobOpportunity.application_method == method)
    if include_remote is False:
        filters.append(
            or_(
                JobOpportunity.workplace_mode.is_(None),  # type: ignore[union-attr]
                JobOpportunity.workplace_mode != "remoto",
            )
        )
    if recency in {"24h", "7d", "14d"}:
        now_utc = datetime.now(timezone.utc)
        delta = (
            timedelta(hours=24)
            if recency == "24h"
            else timedelta(days=7, hours=12)
            if recency == "7d"
            else timedelta(days=14, hours=12)
        )
        cutoff_iso = to_canonical_utc_iso(now_utc - delta)
        filters.append(
            func.coalesce(JobOpportunity.published_at, JobOpportunity.discovered_at) >= cutoff_iso
        )
    if search:
        like_expr = f"%{search.strip()}%"
        filters.append(
            or_(
                col(JobOpportunity.title).ilike(like_expr),
                col(JobOpportunity.company).ilike(like_expr),
                col(JobOpportunity.location).ilike(like_expr),
            )
        )

    count_stmt = select(func.count()).select_from(JobOpportunity)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = session.exec(count_stmt).one()

    query = select(JobOpportunity)
    if filters:
        query = query.where(*filters)
    query = (
        query.order_by(
            col(JobOpportunity.published_at).is_(None).asc(),
            col(JobOpportunity.published_at).desc(),
            col(JobOpportunity.discovered_at).desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    rows = session.exec(query).all()

    items = [
        {
            "id": r.id,
            "company": r.company,
            "title": r.title,
            "original_url": r.original_url,
            "canonical_url": r.canonical_url,
            "primary_source": r.primary_source,
            "source_label": build_source_label(r.primary_source, r.original_url),
            "location": r.location,
            "workplace_mode": r.workplace_mode,
            "contract_type": r.contract_type,
            "opportunity_type": r.opportunity_type,
            "area_id": r.area_id,
            "career_tier": r.career_tier,
            "classification": r.classification,
            "professional_compatibility": r.professional_compatibility,
            "operational_status": r.operational_status,
            "relevance_score": r.relevance_score,
            "practical_priority": r.practical_priority,
            "validity_status": r.validity_status,
            "application_method": r.application_method,
            "application_effort": r.application_effort,
            "automation_capability": r.automation_capability,
            "service_tier": r.service_tier,
            "application_stage": r.application_stage,
            "published_at": r.published_at,
            "published_ago": format_published_ago_pt(r.published_at),
            "last_seen_at": r.last_seen_at,
            "labels": build_job_human_labels(
                classification=r.classification,
                compatibility=r.professional_compatibility,
                operational_status=r.operational_status,
                automation_capability=r.automation_capability,
                stage=r.application_stage,
                workplace_mode=r.workplace_mode,
                career_tier=r.career_tier,
                service_tier=r.service_tier,
            ),
        }
        for r in rows
    ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/jobs/{opportunity_id}")
def get_job_detail(
    opportunity_id: str,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    opp = session.get(JobOpportunity, opportunity_id)
    if not opp:
        raise HTTPException(status_code=404, detail="Job opportunity not found.")

    tracking = session.get(ApplicationTracking, opportunity_id)
    observations = session.exec(
        select(JobObservation)
        .where(JobObservation.opportunity_id == opportunity_id)
        .order_by(col(JobObservation.observed_at).desc())
    ).all()

    opp_dict = opp.model_dump(mode="json")
    opp_dict["published_ago"] = format_published_ago_pt(opp.published_at)
    opp_dict["source_label"] = build_source_label(opp.primary_source, opp.original_url)
    opp_dict["labels"] = build_job_human_labels(
        classification=opp.classification,
        compatibility=opp.professional_compatibility,
        operational_status=opp.operational_status,
        automation_capability=opp.automation_capability,
        stage=opp.application_stage,
        workplace_mode=opp.workplace_mode,
        career_tier=opp.career_tier,
        service_tier=opp.service_tier,
    )

    return {
        "opportunity": opp_dict,
        "requirements": json.loads(opp.requirements_json or "[]"),
        "analysis": json.loads(opp.analysis_json or "{}"),
        "tracking": tracking.model_dump(mode="json") if tracking else None,
        "observations": [obs.model_dump(mode="json") for obs in observations],
    }


@router.post("/jobs/triage-preview", response_model=TriageResult)
def preview_deterministic_triage(
    payload: RawJobInput,
    settings: AppSettings = Depends(get_app_settings),
) -> TriageResult:
    profile, criteria, blocked, _ = load_domain_configs(settings)
    engine = DeterministicTriageEngine(criteria=criteria, blocked=blocked, profile=profile)
    return engine.evaluate(payload)


@router.post("/jobs/import")
def import_job_endpoint(
    payload: AssistedJobImportRequest,
    session: Session = Depends(get_db_session),
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    try:
        return import_assisted_job(session=session, settings=settings, payload=payload)
    except ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/discovery/run")
async def trigger_discovery_run(
    request: Request,
    payload: DiscoveryRunRequest | None = None,
    settings: AppSettings = Depends(get_app_settings),
    scheduler: DiscoverySchedulerManager = Depends(get_scheduler_manager),
) -> dict[str, Any]:
    req = payload or DiscoveryRunRequest()
    if scheduler.run_lock.locked():
        raise HTTPException(status_code=409, detail="A discovery run is already in progress.")

    mock_transport = getattr(request.app.state, "http_transport", None)
    llm_transport = getattr(request.app.state, "llm_transport", None)
    rate_limit = 0.0 if mock_transport is not None else 0.35

    async with scheduler.run_lock:
        summary = await execute_discovery_run(
            db_engine=request.app.state.engine,
            settings=settings,
            max_requests_budget=req.max_requests_budget,
            source_ids=req.source_ids,
            transport=mock_transport,
            llm_transport=llm_transport,
            default_rate_limit_seconds=rate_limit,
        )
        scheduler.last_run_summary = summary
    return summary


@router.get("/discovery/runs")
def list_discovery_runs(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = session.exec(
        select(DiscoveryRun)
        .order_by(col(DiscoveryRun.started_at).desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "run_kind": r.run_kind,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "extracted_count": r.extracted_count,
            "unique_count": r.unique_count,
            "new_count": r.new_count,
            "summary": json.loads(r.summary_json or "{}"),
        }
        for r in rows
    ]


@router.get("/sources")
def get_sources(
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    _, _, _, sources = load_domain_configs(settings)
    return sources.model_dump(mode="json")


@router.patch("/sources/{source_id}")
def toggle_source(
    source_id: str,
    payload: SourceToggleRequest,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    raw = json.loads(settings.sources_path.read_text(encoding="utf-8"))
    matched = None
    for entry in raw.get("sources", []):
        if entry.get("id") == source_id:
            entry["enabled"] = payload.enabled
            matched = entry
            break
    if matched is None:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found.")

    validated = JobSourcesConfig.model_validate(raw)
    backup_name = atomic_write_validated_json(
        target_path=settings.sources_path,
        validated_model=validated,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    return {"id": source_id, "enabled": payload.enabled, "backup_snapshot": backup_name}


@router.get("/scheduler")
def read_scheduler_status(
    scheduler: DiscoverySchedulerManager = Depends(get_scheduler_manager),
) -> dict[str, Any]:
    return scheduler.status()


@router.post("/scheduler")
def update_scheduler_config(
    payload: SchedulerConfigRequest,
    scheduler: DiscoverySchedulerManager = Depends(get_scheduler_manager),
) -> dict[str, Any]:
    try:
        return scheduler.configure(
            enabled=payload.enabled,
            interval_minutes=payload.interval_minutes,
            max_requests_budget=payload.max_requests_budget,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
