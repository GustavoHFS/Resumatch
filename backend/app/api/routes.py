"""Core REST API endpoints (`/api/health`, `/api/profile`, `/api/config/*`) with atomic writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.core.config import AppSettings
from app.core.database import get_db_session
from app.models import (
    ApplicationTracking,
    BlockedCompaniesConfig,
    DiscoveryRun,
    JobOpportunity,
    JobSourcesConfig,
    MasterProfile,
    ResumeVersion,
    SearchCriteria,
)
from app.services.config_service import atomic_write_validated_json

router = APIRouter(prefix="/api", tags=["core"])


def get_app_settings(request: Request) -> AppSettings:
    return request.app.state.settings


def _load_validated_json(path: Path, schema_cls: Any, resource_name: str) -> Any:
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"{resource_name} file not found in local workspace.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return schema_cls.model_validate(payload)
    except (OSError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {resource_name} schema: {exc}",
        ) from exc


@router.get("/health")
def health_check(
    request: Request,
    session: Session = Depends(get_db_session),
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    journal_mode = str(
        session.exec(text("PRAGMA journal_mode")).scalar() or ""
    ).lower()
    opportunities_count = session.exec(
        select(func.count()).select_from(JobOpportunity)
    ).one()
    versions_count = session.exec(
        select(func.count()).select_from(ResumeVersion)
    ).one()
    tracking_count = session.exec(
        select(func.count()).select_from(ApplicationTracking)
    ).one()
    runs_count = session.exec(
        select(func.count()).select_from(DiscoveryRun)
    ).one()

    return {
        "status": "ok",
        "project": settings.project_name,
        "version": settings.version,
        "sqlite_journal_mode": journal_mode,
        "scheduler_enabled": settings.scheduler_enabled,
        "llm_provider": settings.llm_provider,
        "bootstrapped_demo": bool(getattr(request.app.state, "bootstrapped_demo", False)),
        "counts": {
            "opportunities": opportunities_count,
            "resume_versions": versions_count,
            "tracked_applications": tracking_count,
            "discovery_runs": runs_count,
        },
    }


@router.get("/profile", response_model=MasterProfile)
def read_master_profile(
    settings: AppSettings = Depends(get_app_settings),
) -> MasterProfile:
    return _load_validated_json(settings.profile_path, MasterProfile, "MasterProfile")


@router.put("/profile")
def update_master_profile(
    payload: MasterProfile,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    backup_name = atomic_write_validated_json(
        target_path=settings.profile_path,
        validated_model=payload,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    return {
        "status": "updated",
        "backup_snapshot": backup_name,
        "profile": payload.model_dump(mode="json"),
    }


@router.get("/config/criteria", response_model=SearchCriteria)
def read_search_criteria(
    settings: AppSettings = Depends(get_app_settings),
) -> SearchCriteria:
    return _load_validated_json(settings.criteria_path, SearchCriteria, "SearchCriteria")


@router.put("/config/criteria")
def update_search_criteria(
    payload: SearchCriteria,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    backup_name = atomic_write_validated_json(
        target_path=settings.criteria_path,
        validated_model=payload,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    return {
        "status": "updated",
        "backup_snapshot": backup_name,
        "criteria": payload.model_dump(mode="json"),
    }


@router.get("/config/blocked", response_model=BlockedCompaniesConfig)
def read_blocked_companies(
    settings: AppSettings = Depends(get_app_settings),
) -> BlockedCompaniesConfig:
    return _load_validated_json(
        settings.blocked_companies_path,
        BlockedCompaniesConfig,
        "BlockedCompaniesConfig",
    )


@router.put("/config/blocked")
def update_blocked_companies(
    payload: BlockedCompaniesConfig,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    backup_name = atomic_write_validated_json(
        target_path=settings.blocked_companies_path,
        validated_model=payload,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    return {
        "status": "updated",
        "backup_snapshot": backup_name,
        "blocked": payload.model_dump(mode="json"),
    }


@router.get("/config/sources", response_model=JobSourcesConfig)
def read_job_sources(
    settings: AppSettings = Depends(get_app_settings),
) -> JobSourcesConfig:
    return _load_validated_json(settings.sources_path, JobSourcesConfig, "JobSourcesConfig")
