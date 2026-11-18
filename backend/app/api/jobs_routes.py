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
from app.core.database import get_db_session
from app.domain.triage_engine import DeterministicTriageEngine, RawJobInput, TriageResult
from app.models import (
    ApplicationTracking,
    DiscoveryRun,
    JobObservation,
    JobOpportunity,
    JobSourcesConfig,
)
from app.models.profile_schema import StrictModel
from app.services.discovery_service import (
    AssistedJobImportRequest,
    execute_discovery_run,
    import_assisted_job,
    load_domain_configs,
)
from app.services.scheduler_service import DiscoverySchedulerManager

router = APIRouter(prefix="/api", tags=["discovery-and-triage"])


class DiscoveryRunRequest(StrictModel):
    max_requests_budget: int = Field(default=40, ge=1, le=150)
    source_ids: list[str] | None = None


class SourceToggleRequest(StrictModel):
    enabled: bool


class SchedulerConfigRequest(StrictModel):
    enabled: bool
    interval_minutes: int | None = Field(default=None, ge=15, le=10080)
    max_requests_budget: int | None = Field(default=None, ge=5, le=150)


def get_scheduler_manager(request: Request) -> DiscoverySchedulerManager:
    return request.app.state.scheduler


@router.get("/jobs")
def list_jobs(
    area_id: str | None = None,
    classification: str | None = None,
    compatibility: str | None = None,
    stage: str | None = None,
    service_tier: int | None = Query(default=None, ge=1, le=3),
    method: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    filters = []
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
            col(JobOpportunity.practical_priority).desc(),
            col(JobOpportunity.last_seen_at).desc(),
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
            "last_seen_at": r.last_seen_at,
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

    return {
        "opportunity": opp.model_dump(mode="json"),
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
    rate_limit = 0.0 if mock_transport is not None else 0.35

    async with scheduler.run_lock:
        summary = await execute_discovery_run(
            db_engine=request.app.state.engine,
            settings=settings,
            max_requests_budget=req.max_requests_budget,
            source_ids=req.source_ids,
            transport=mock_transport,
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
    settings.sources_path.write_text(
        json.dumps(validated.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"id": source_id, "enabled": payload.enabled}


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
