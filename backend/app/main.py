"""FastAPI application factory and lifespan manager for Resumatch."""

from __future__ import annotations

import time

import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.adapters.pdf.browser_manager import ChromiumPdfBrowserManager
from app.api.applications_routes import router as applications_router
from app.api.jobs_routes import router as jobs_router
from app.api.resumes_routes import router as resumes_router
from app.api.routes import PersistedLlmSettings, router as core_router
from app.core.config import AppSettings, get_settings
from app.core.database import create_sqlite_wal_engine, init_db_schema
from app.services.scheduler_service import DiscoverySchedulerManager

EXAMPLE_BOOTSTRAP_PAIRS = (
    ("master_profile.example.json", "profile/master_profile.json"),
    ("search_criteria.example.json", "config/search_criteria.json"),
    ("blocked_companies.example.json", "config/blocked_companies.json"),
    ("sources.example.json", "config/sources.json"),
)


def _ensure_workspace_files(settings: AppSettings) -> bool:
    """Ensures `data_dir` subdirectories exist; copies demo examples ONLY if missing."""
    data_dir = settings.safe_data_dir
    for folder in ("profile", "config", "db", "exports", "backups"):
        (data_dir / folder).mkdir(parents=True, exist_ok=True)

    bootstrapped = False
    for example_name, target_rel in EXAMPLE_BOOTSTRAP_PAIRS:
        src = settings.examples_dir / example_name
        dst = data_dir / target_rel
        if not dst.exists() and src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            bootstrapped = True
    return bootstrapped


def _apply_persisted_llm_overrides(settings: AppSettings) -> None:
    """Auto-activates saved Google Gemini key from `user_data/config/llm_settings.json` on startup."""
    llm_file = settings.llm_settings_path
    if not llm_file.is_file():
        return
    try:
        persisted = PersistedLlmSettings.model_validate(
            json.loads(llm_file.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return
    if persisted.gemini_model:
        settings.gemini_model = persisted.gemini_model
    if persisted.gemini_api_key:
        settings.gemini_api_key = persisted.gemini_api_key


def _mount_spa_frontend(application: FastAPI, dist_dir: Path) -> None:
    """Serves Vite static build and SPA fallback when `frontend/dist/index.html` exists."""
    resolved_dist = dist_dir.resolve()
    index_file = resolved_dist / "index.html"
    if not index_file.is_file():
        return

    assets_dir = resolved_dist / "assets"
    if assets_dir.is_dir():
        application.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir)),
            name="spa-assets",
        )

    no_cache_headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    @application.get("/", include_in_schema=False)
    def serve_spa_root() -> FileResponse:
        return FileResponse(index_file, media_type="text/html", headers=no_cache_headers)

    @application.get("/{full_path:path}", include_in_schema=False)
    def serve_spa_fallback(full_path: str) -> FileResponse:
        normalized = full_path.strip("/")
        if normalized == "api" or normalized.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found.")

        if normalized:
            candidate = (resolved_dist / normalized).resolve()
            if resolved_dist in candidate.parents and candidate.is_file():
                return FileResponse(candidate)

        return FileResponse(index_file, media_type="text/html", headers=no_cache_headers)


def create_app(
    settings: AppSettings | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bootstrapped = _ensure_workspace_files(resolved_settings)
        _apply_persisted_llm_overrides(resolved_settings)
        engine = create_sqlite_wal_engine(resolved_settings.db_path)
        journal_mode = init_db_schema(engine)
        scheduler = DiscoverySchedulerManager(db_engine=engine, settings=resolved_settings)
        scheduler.start()
        pdf_browser_manager = ChromiumPdfBrowserManager(
            idle_timeout_seconds=resolved_settings.pdf_browser_idle_timeout_seconds
        )

        application.state.settings = resolved_settings
        application.state.engine = engine
        application.state.journal_mode = journal_mode
        application.state.bootstrapped_demo = bootstrapped
        application.state.scheduler = scheduler
        application.state.pdf_browser_manager = pdf_browser_manager
        application.state.http_transport = http_transport
        application.state.llm_transport = llm_transport
        application.state.lifecycle = {
            "active_clients": {},
            "booted_at": time.monotonic(),
            "ever_connected": False,
            "last_seen_at": None,
            "close_requested_at": None,
            "closed_signal": False,
        }
        try:
            yield
        finally:
            await pdf_browser_manager.aclose()
            scheduler.shutdown()
            engine.dispose()

    application = FastAPI(
        title=resolved_settings.project_name,
        version=resolved_settings.version,
        description="Local-first deterministic job triage & ATS-clean A4 resume studio.",
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved_settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(core_router)
    application.include_router(jobs_router)
    application.include_router(resumes_router)
    application.include_router(applications_router)
    _mount_spa_frontend(application, resolved_settings.frontend_dist_dir)
    return application


app = create_app()
