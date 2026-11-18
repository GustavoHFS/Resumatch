"""FastAPI application factory and lifespan manager for Resumatch."""

from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.pdf.browser_manager import ChromiumPdfBrowserManager
from app.api.applications_routes import router as applications_router
from app.api.jobs_routes import router as jobs_router
from app.api.resumes_routes import router as resumes_router
from app.api.routes import router as core_router
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


def create_app(
    settings: AppSettings | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bootstrapped = _ensure_workspace_files(resolved_settings)
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
    return application


app = create_app()
