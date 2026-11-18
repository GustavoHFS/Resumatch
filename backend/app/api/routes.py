"""Core REST API endpoints (`/api/health`, `/api/profile`, `/api/config/*`) with atomic writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, ValidationError
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
from app.models.profile_schema import StrictModel
from app.services.config_service import atomic_write_validated_json

router = APIRouter(prefix="/api", tags=["core"])


class LlmSettingsUpdateRequest(StrictModel):
    provider: Literal["none", "gemini", "groq", "ollama"]
    timeout_seconds: float = Field(default=15.0, ge=3.0, le=120.0)
    gemini_model: str = Field(default="gemini-2.5-flash", min_length=2, max_length=120)
    gemini_api_key: str | None = Field(default=None, max_length=300)
    clear_gemini_key: bool = False
    groq_model: str = Field(default="llama-3.3-70b-versatile", min_length=2, max_length=120)
    groq_api_key: str | None = Field(default=None, max_length=300)
    clear_groq_key: bool = False
    ollama_base_url: str = Field(default="http://127.0.0.1:11434", min_length=8, max_length=200)
    ollama_model: str = Field(default="llama3.2", min_length=2, max_length=120)


class PersistedLlmSettings(StrictModel):
    provider: Literal["none", "gemini", "groq", "ollama"] = "none"
    timeout_seconds: float = 15.0
    gemini_model: str = "gemini-2.5-flash"
    gemini_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_api_key: str = ""
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2"


def serialize_masked_llm_settings(settings: AppSettings) -> dict[str, Any]:
    """Serializes active LLM settings without ever exposing raw API keys."""
    return {
        "provider": settings.llm_provider,
        "timeout_seconds": settings.llm_timeout_seconds,
        "gemini_model": settings.gemini_model,
        "gemini_key_configured": bool(settings.gemini_api_key.strip()),
        "groq_model": settings.groq_model,
        "groq_key_configured": bool(settings.groq_api_key.strip()),
        "ollama_base_url": settings.ollama_base_url,
        "ollama_model": settings.ollama_model,
    }


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


@router.put("/config/sources")
def update_job_sources(
    payload: JobSourcesConfig,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    backup_name = atomic_write_validated_json(
        target_path=settings.sources_path,
        validated_model=payload,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    return {
        "status": "updated",
        "backup_snapshot": backup_name,
        "sources": payload.model_dump(mode="json"),
    }


@router.get("/config/llm")
def read_llm_settings(
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    return serialize_masked_llm_settings(settings)


@router.put("/config/llm")
def update_llm_settings(
    payload: LlmSettingsUpdateRequest,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    new_gemini_key = settings.gemini_api_key
    if payload.clear_gemini_key:
        new_gemini_key = ""
    elif payload.gemini_api_key is not None and payload.gemini_api_key.strip():
        new_gemini_key = payload.gemini_api_key.strip()

    new_groq_key = settings.groq_api_key
    if payload.clear_groq_key:
        new_groq_key = ""
    elif payload.groq_api_key is not None and payload.groq_api_key.strip():
        new_groq_key = payload.groq_api_key.strip()

    persisted = PersistedLlmSettings(
        provider=payload.provider,
        timeout_seconds=payload.timeout_seconds,
        gemini_model=payload.gemini_model,
        gemini_api_key=new_gemini_key,
        groq_model=payload.groq_model,
        groq_api_key=new_groq_key,
        ollama_base_url=payload.ollama_base_url,
        ollama_model=payload.ollama_model,
    )
    backup_name = atomic_write_validated_json(
        target_path=settings.llm_settings_path,
        validated_model=persisted,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )

    settings.llm_provider = persisted.provider
    settings.llm_timeout_seconds = persisted.timeout_seconds
    settings.gemini_model = persisted.gemini_model
    settings.gemini_api_key = persisted.gemini_api_key
    settings.groq_model = persisted.groq_model
    settings.groq_api_key = persisted.groq_api_key
    settings.ollama_base_url = persisted.ollama_base_url
    settings.ollama_model = persisted.ollama_model

    return {
        "status": "updated",
        "backup_snapshot": backup_name,
        "llm": serialize_masked_llm_settings(settings),
    }
