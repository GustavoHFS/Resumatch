"""Kanban Application Lifecycle REST API (`/api/applications`).

Enforces audited state transitions, prevents simulated automated submissions, requires
linked `ResumeVersion` (with immutable `pdf_sha256`) for prepared/submitted stages, and
requires explicit `confirm_manual_submission=True` when marking `enviada_manualmente`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import func
from sqlmodel import Session, col, or_, select

from app.core.database import get_db_session
from app.domain.labels_pt import STAGE_PT, build_job_human_labels, humanize_code
from app.models import (
    ApplicationEvent,
    ApplicationTracking,
    JobOpportunity,
    ResumeVersion,
)
from app.models.profile_schema import StrictModel

router = APIRouter(prefix="/api/applications", tags=["applications"])

KanbanStage = Literal[
    "descoberta",
    "candidatura_a_preparar",
    "curriculo_preparado",
    "aguardando_envio_manual",
    "enviada_manualmente",
    "encerrada_ou_descartada",
]

ALL_STAGES: set[str] = {
    "descoberta",
    "candidatura_a_preparar",
    "curriculo_preparado",
    "aguardando_envio_manual",
    "enviada_manualmente",
    "encerrada_ou_descartada",
}

ALLOWED_STAGE_TRANSITIONS: dict[str, set[str]] = {
    stage: ALL_STAGES - {stage} for stage in ALL_STAGES
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApplicationCreateRequest(StrictModel):
    opportunity_id: str = Field(min_length=3, max_length=120)
    stage: Literal["descoberta", "candidatura_a_preparar"] = "candidatura_a_preparar"
    notes: str | None = Field(default=None, max_length=500)


class ApplicationStagePatchRequest(StrictModel):
    stage: str = Field(min_length=3, max_length=60)
    resume_version_id: str | None = Field(default=None, max_length=120)
    confirm_manual_submission: bool = False
    notes: str | None = Field(default=None, max_length=500)


def _serialize_application_card(
    tracking: ApplicationTracking,
    opp: JobOpportunity,
    version: ResumeVersion | None,
) -> dict[str, Any]:
    return {
        "opportunity_id": tracking.opportunity_id,
        "updated_at": tracking.updated_at,
        "stage": tracking.stage,
        "stage_label": humanize_code(STAGE_PT, tracking.stage, "Descoberta"),
        "labels": build_job_human_labels(
            classification=opp.classification,
            compatibility=tracking.professional_compatibility or opp.professional_compatibility,
            operational_status=tracking.operational_status or opp.operational_status,
            automation_capability=tracking.automation_capability or opp.automation_capability,
            stage=tracking.stage,
            workplace_mode=opp.workplace_mode,
            career_tier=opp.career_tier,
            service_tier=tracking.service_tier or opp.service_tier,
        ),
        "queue_state": tracking.queue_state,
        "professional_compatibility": tracking.professional_compatibility or opp.professional_compatibility,
        "operational_status": tracking.operational_status or opp.operational_status,
        "service_tier": tracking.service_tier or opp.service_tier,
        "automation_capability": tracking.automation_capability or opp.automation_capability,
        "application_method": tracking.application_method or opp.application_method,
        "pdf_status": tracking.pdf_status,
        "submission_instructions": tracking.submission_instructions,
        "user_confirmed_at": tracking.user_confirmed_at,
        "notes": tracking.notes,
        "job": {
            "title": opp.title,
            "company": opp.company,
            "original_url": opp.original_url,
            "location": opp.location,
            "workplace_mode": opp.workplace_mode,
            "area_id": opp.area_id,
            "classification": opp.classification,
            "practical_priority": opp.practical_priority,
        },
        "resume_version": (
            {
                "id": version.id,
                "area_id": version.area_id,
                "generation_mode": version.generation_mode,
                "pdf_sha256": version.pdf_sha256,
                "export_rel_path": version.export_rel_path,
                "created_at": version.created_at,
            }
            if version
            else None
        ),
    }


@router.get("")
def list_applications(
    stage: str | None = None,
    area_id: str | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    stmt = (
        select(ApplicationTracking, JobOpportunity)
        .join(JobOpportunity, JobOpportunity.id == ApplicationTracking.opportunity_id)
    )
    count_stmt = (
        select(func.count())
        .select_from(ApplicationTracking)
        .join(JobOpportunity, JobOpportunity.id == ApplicationTracking.opportunity_id)
    )

    filters = []
    if stage:
        filters.append(ApplicationTracking.stage == stage)
    if area_id:
        filters.append(JobOpportunity.area_id == area_id)
    if search:
        like_expr = f"%{search.strip()}%"
        filters.append(
            or_(
                col(JobOpportunity.title).ilike(like_expr),
                col(JobOpportunity.company).ilike(like_expr),
            )
        )

    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)

    total = session.exec(count_stmt).one()
    rows = session.exec(
        stmt.order_by(col(ApplicationTracking.updated_at).desc())
        .offset(offset)
        .limit(limit)
    ).all()

    version_ids = {t.resume_version_id for t, _ in rows if t.resume_version_id}
    versions_by_id: dict[str, ResumeVersion] = {}
    if version_ids:
        v_rows = session.exec(
            select(ResumeVersion).where(col(ResumeVersion.id).in_(version_ids))
        ).all()
        versions_by_id = {v.id: v for v in v_rows}

    items = [
        _serialize_application_card(
            t,
            opp,
            versions_by_id.get(t.resume_version_id) if t.resume_version_id else None,
        )
        for t, opp in rows
    ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.post("", status_code=201)
def create_or_enqueue_application(
    payload: ApplicationCreateRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    opp = session.get(JobOpportunity, payload.opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Job opportunity not found.")

    now_iso = _utc_now()
    tracking = session.get(ApplicationTracking, opp.id)
    if tracking is None:
        tracking = ApplicationTracking(
            opportunity_id=opp.id,
            updated_at=now_iso,
            stage=payload.stage,
            queue_state="candidatura_a_preparar",
            professional_compatibility=opp.professional_compatibility,
            operational_status=opp.operational_status,
            service_tier=opp.service_tier,
            automation_capability=opp.automation_capability,
            application_method=opp.application_method,
            notes=payload.notes or "Enqueued for application preparation",
        )
        session.add(tracking)
    else:
        tracking.updated_at = now_iso
        if tracking.stage == "descoberta" and payload.stage == "candidatura_a_preparar":
            tracking.stage = payload.stage
        if payload.notes:
            tracking.notes = payload.notes
        session.add(tracking)

    if opp.application_stage == "descoberta" and payload.stage == "candidatura_a_preparar":
        opp.application_stage = payload.stage
        session.add(opp)

    session.add(
        ApplicationEvent(
            opportunity_id=opp.id,
            created_at=now_iso,
            event_source="tracking",
            stage=tracking.stage,
            notes=payload.notes or "Application tracking initialized",
        )
    )
    session.commit()
    session.refresh(tracking)

    version = session.get(ResumeVersion, tracking.resume_version_id) if tracking.resume_version_id else None
    return _serialize_application_card(tracking, opp, version)


@router.patch("/{opportunity_id}/stage")
def transition_application_stage(
    opportunity_id: str,
    payload: ApplicationStagePatchRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    if payload.stage == "enviada_automaticamente":
        raise HTTPException(
            status_code=422,
            detail="Automated submission ('enviada_automaticamente') is prohibited by project governance.",
        )
    if payload.stage not in ALLOWED_STAGE_TRANSITIONS:
        raise HTTPException(status_code=422, detail=f"Invalid application stage '{payload.stage}'.")

    opp = session.get(JobOpportunity, opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Job opportunity not found.")

    tracking = session.get(ApplicationTracking, opportunity_id)
    current_stage = tracking.stage if tracking else (opp.application_stage or "descoberta")

    if payload.stage != current_stage and payload.stage not in ALLOWED_STAGE_TRANSITIONS.get(current_stage, set()):
        raise HTTPException(
            status_code=422,
            detail=f"Disallowed stage transition: '{current_stage}' -> '{payload.stage}'.",
        )

    effective_version_id = payload.resume_version_id or (tracking.resume_version_id if tracking else None)
    version: ResumeVersion | None = None
    if effective_version_id:
        version = session.get(ResumeVersion, effective_version_id)
        if version is None:
            raise HTTPException(
                status_code=422,
                detail=f"ResumeVersion '{effective_version_id}' does not exist.",
            )

    if payload.stage == "enviada_manualmente" and not payload.confirm_manual_submission:
        raise HTTPException(
            status_code=422,
            detail="Confirme o envio manual antes de marcar como enviada.",
        )

    now_iso = _utc_now()
    if tracking is None:
        tracking = ApplicationTracking(
            opportunity_id=opp.id,
            updated_at=now_iso,
            stage=payload.stage,
            resume_version_id=version.id if version else None,
            service_tier=opp.service_tier,
            automation_capability=opp.automation_capability,
            application_method=opp.application_method,
            notes=payload.notes,
        )
    else:
        tracking.updated_at = now_iso
        tracking.stage = payload.stage
        if version is not None:
            tracking.resume_version_id = version.id
        if payload.notes is not None:
            tracking.notes = payload.notes

    if payload.stage == "enviada_manualmente":
        tracking.user_confirmed_at = now_iso

    opp.application_stage = payload.stage
    session.add(tracking)
    session.add(opp)

    sha_note = f" (PDF SHA-256: {version.pdf_sha256[:12]})" if version and version.pdf_sha256 else ""
    session.add(
        ApplicationEvent(
            opportunity_id=opp.id,
            created_at=now_iso,
            event_source="tracking",
            stage=payload.stage,
            notes=(payload.notes or f"Stage transitioned from '{current_stage}' to '{payload.stage}'") + sha_note,
        )
    )
    session.commit()
    session.refresh(tracking)
    return _serialize_application_card(tracking, opp, version)


@router.get("/{opportunity_id}/events")
def list_application_events(
    opportunity_id: str,
    session: Session = Depends(get_db_session),
) -> list[dict[str, Any]]:
    opp = session.get(JobOpportunity, opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Job opportunity not found.")

    events = session.exec(
        select(ApplicationEvent)
        .where(ApplicationEvent.opportunity_id == opportunity_id)
        .order_by(col(ApplicationEvent.created_at).asc(), col(ApplicationEvent.id).asc())
    ).all()
    return [ev.model_dump(mode="json") for ev in events]


@router.delete("/{opportunity_id}")
def delete_application_tracking(
    opportunity_id: str,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    tracking = session.get(ApplicationTracking, opportunity_id)
    if tracking is None:
        raise HTTPException(status_code=404, detail="Candidatura não encontrada.")

    events = session.exec(
        select(ApplicationEvent).where(ApplicationEvent.opportunity_id == opportunity_id)
    ).all()
    for ev in events:
        session.delete(ev)

    session.delete(tracking)

    opp = session.get(JobOpportunity, opportunity_id)
    if opp is not None:
        opp.application_stage = "descoberta"
        session.add(opp)

    session.commit()
    return {"status": "deleted", "opportunity_id": opportunity_id}
