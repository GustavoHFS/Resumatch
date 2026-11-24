"""Core REST API endpoints (`/api/health`, `/api/profile`, `/api/config/*`) with atomic writes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.adapters.llm.providers import GeminiAdapter, LlmAdapterError
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
from app.services.profile_import_service import (
    ProfileImportError,
    import_profile_from_uploaded_file,
)
from app.services.discovery_service import reconcile_triage_in_sqlite

router = APIRouter(prefix="/api", tags=["core"])

# Long inactivity window: the launcher must never kill the backend because of a short
# ping delay (minimized window, sleeping laptop, reload, network blip). Only a real
# close beacon or a full 15-minute silence triggers shutdown.
CLIENT_INACTIVITY_SECONDS = 30 * 60.0
BOOT_GRACE_SECONDS = 120.0
CLOSE_GRACE_SECONDS = 10.0


class GoogleConnectRequest(StrictModel):
    api_key: str = Field(min_length=10, max_length=300)


class LifecyclePingRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_id: str = Field(default="default", min_length=1, max_length=120)


class PersistedLlmSettings(BaseModel):
    """Tolerates legacy fields via extra='ignore' while persisting only Google Gemini config."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"


def serialize_masked_llm_settings(settings: AppSettings) -> dict[str, Any]:
    """Serializes Google connection status without ever exposing the raw key."""
    return {
        "connected": settings.ai_connected,
        "model": settings.gemini_model,
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
        "ai_connected": settings.ai_connected,
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
    request: Request,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    backup_name = atomic_write_validated_json(
        target_path=settings.profile_path,
        validated_model=payload,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    reconcile_triage_in_sqlite(request.app.state.engine, settings, create_backup=False, fast=True)
    return {
        "status": "updated",
        "backup_snapshot": backup_name,
        "profile": payload.model_dump(mode="json"),
    }


@router.post("/profile/import-file")
async def import_master_profile_file(
    request: Request,
    file: UploadFile = File(...),
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    llm_transport = getattr(request.app.state, "llm_transport", None)
    raw_bytes = await file.read()
    try:
        return await import_profile_from_uploaded_file(
            filename=file.filename or "",
            raw_bytes=raw_bytes,
            settings=settings,
            llm_transport=llm_transport,
        )
    except ProfileImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/config/criteria", response_model=SearchCriteria)
def read_search_criteria(
    settings: AppSettings = Depends(get_app_settings),
) -> SearchCriteria:
    return _load_validated_json(settings.criteria_path, SearchCriteria, "SearchCriteria")


@router.put("/config/criteria")
def update_search_criteria(
    payload: SearchCriteria,
    request: Request,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    backup_name = atomic_write_validated_json(
        target_path=settings.criteria_path,
        validated_model=payload,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    reconcile_triage_in_sqlite(request.app.state.engine, settings, create_backup=False, fast=True)
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
    request: Request,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    backup_name = atomic_write_validated_json(
        target_path=settings.blocked_companies_path,
        validated_model=payload,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    reconcile_triage_in_sqlite(request.app.state.engine, settings, create_backup=False, fast=True)
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


@router.post("/config/llm/connect")
async def connect_google_ai(
    request: Request,
    payload: GoogleConnectRequest,
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    candidate_key = payload.api_key.strip()
    llm_transport = getattr(request.app.state, "llm_transport", None)
    adapter = GeminiAdapter(
        api_key=candidate_key,
        model=settings.gemini_model,
        fallback_model=settings.gemini_fallback_model,
        timeout_seconds=settings.llm_timeout_seconds,
        transport=llm_transport,
    )
    try:
        verified_model = await adapter.validate_connection()
    except LlmAdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    persisted = PersistedLlmSettings(
        gemini_api_key=candidate_key,
        gemini_model=verified_model,
    )
    backup_name = atomic_write_validated_json(
        target_path=settings.llm_settings_path,
        validated_model=persisted,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    settings.gemini_api_key = candidate_key
    settings.gemini_model = verified_model

    return {
        "status": "connected",
        "backup_snapshot": backup_name,
        "llm": serialize_masked_llm_settings(settings),
    }


@router.delete("/config/llm")
def disconnect_google_ai(
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    persisted = PersistedLlmSettings(
        gemini_api_key="",
        gemini_model=settings.gemini_model,
    )
    atomic_write_validated_json(
        target_path=settings.llm_settings_path,
        validated_model=persisted,
        backups_dir=settings.backups_dir,
        max_backups=settings.max_config_backups,
    )
    settings.gemini_api_key = ""
    return {
        "status": "disconnected",
        "llm": serialize_masked_llm_settings(settings),
    }


def _get_lifecycle_state(request: Request) -> dict[str, Any]:
    state = getattr(request.app.state, "lifecycle", None)
    if state is None:
        state = {
            "active_clients": {},
            "booted_at": time.monotonic(),
            "ever_connected": False,
            "last_seen_at": None,
            "close_requested_at": None,
            "closed_signal": False,
        }
        request.app.state.lifecycle = state
    return state


@router.post("/lifecycle/ping")
def lifecycle_ping(
    request: Request,
    payload: LifecyclePingRequest | None = None,
) -> dict[str, Any]:
    state = _get_lifecycle_state(request)
    cid = (payload.client_id if payload else "default").strip() or "default"
    now = time.monotonic()
    active: dict[str, float] = state["active_clients"]
    active[cid] = now
    # Prune tabs only after the long inactivity window (never a short ping delay).
    for k in [k for k, ts in active.items() if now - ts > CLIENT_INACTIVITY_SECONDS]:
        active.pop(k, None)
    state["ever_connected"] = True
    state["last_seen_at"] = now
    state["closed_signal"] = False
    state["close_requested_at"] = None
    return {"status": "ok", "active_clients": len(active)}


@router.post("/lifecycle/close")
async def lifecycle_close(request: Request) -> dict[str, Any]:
    state = _get_lifecycle_state(request)
    cid = "default"
    try:
        raw_body = await request.body()
        if raw_body:
            parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
            if isinstance(parsed, dict) and parsed.get("client_id"):
                cid = str(parsed["client_id"]).strip()
    except Exception:
        pass
    active: dict[str, float] = state["active_clients"]
    active.pop(cid, None)
    if not active:
        state["closed_signal"] = True
        state["close_requested_at"] = time.monotonic()
    return {"status": "closing", "active_clients": len(active)}


@router.get("/lifecycle/status")
def lifecycle_status(request: Request) -> dict[str, Any]:
    state = _get_lifecycle_state(request)
    now = time.monotonic()
    active: dict[str, float] = state["active_clients"]
    for k in [k for k, ts in active.items() if now - ts > CLIENT_INACTIVITY_SECONDS]:
        active.pop(k, None)
    last_seen = state.get("last_seen_at")
    seconds_since = round(now - last_seen, 2) if isinstance(last_seen, (int, float)) else None
    boot_age = now - state["booted_at"]
    close_requested_at = state["close_requested_at"]
    shutdown_ready = boot_age >= BOOT_GRACE_SECONDS and (
        (close_requested_at is not None and now - close_requested_at >= CLOSE_GRACE_SECONDS and not active)
        or (last_seen is not None and now - last_seen >= CLIENT_INACTIVITY_SECONDS and not active)
        or (last_seen is None and boot_age >= CLIENT_INACTIVITY_SECONDS)
    )
    return {
        "ever_connected": bool(state["ever_connected"]),
        "active_clients": len(active),
        "seconds_since_last_seen": seconds_since,
        "inactivity_timeout_seconds": int(CLIENT_INACTIVITY_SECONDS),
        "boot_grace_seconds": int(BOOT_GRACE_SECONDS),
        "close_grace_seconds": int(CLOSE_GRACE_SECONDS),
        "close_requested_at": close_requested_at,
        "shutdown_ready": shutdown_ready,
        "closed_signal": bool(state["closed_signal"]),
    }
