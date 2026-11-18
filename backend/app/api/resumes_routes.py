"""Phase 3 REST endpoints for deterministic resume composition, HTML preview, and A4 PDF export."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session

from app.adapters.pdf.browser_manager import ChromiumPdfBrowserManager, PdfRenderError
from app.adapters.pdf.html_renderer import render_resume_a4_html
from app.api.routes import get_app_settings
from app.core.config import AppSettings
from app.core.database import get_db_session
from app.services.resume_service import (
    ResumeComposeRequest,
    compose_resume_payload,
    export_validated_pdf,
    list_resume_versions,
)

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


def get_pdf_browser_manager(request: Request) -> ChromiumPdfBrowserManager:
    return request.app.state.pdf_browser_manager


@router.post("/compose")
def compose_resume_endpoint(
    payload: ResumeComposeRequest,
    session: Session = Depends(get_db_session),
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        composed, triage, _, _, _ = compose_resume_payload(session, settings, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 3)
    return {
        "elapsed_ms": elapsed_ms,
        "payload": composed.model_dump(mode="json"),
        "triage": triage.model_dump(mode="json") if triage else None,
    }


@router.post("/render-html")
def render_resume_html_endpoint(
    payload: ResumeComposeRequest,
    session: Session = Depends(get_db_session),
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    try:
        composed, _, _, _, _ = compose_resume_payload(session, settings, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    html = render_resume_a4_html(composed, standalone_preview=payload.standalone_preview)
    return {
        "area_id": composed.area_id,
        "locale": composed.locale,
        "max_pages": composed.max_pages,
        "html": html,
        "payload": composed.model_dump(mode="json"),
    }


@router.post("/export-pdf")
async def export_resume_pdf_endpoint(
    payload: ResumeComposeRequest,
    session: Session = Depends(get_db_session),
    settings: AppSettings = Depends(get_app_settings),
    pdf_manager: ChromiumPdfBrowserManager = Depends(get_pdf_browser_manager),
) -> dict[str, Any]:
    try:
        return await export_validated_pdf(
            session=session,
            settings=settings,
            pdf_renderer=pdf_manager,
            req=payload,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PdfRenderError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/versions")
def get_resume_versions_endpoint(
    area_id: str | None = None,
    opportunity_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db_session),
) -> list[dict[str, Any]]:
    return list_resume_versions(
        session,
        area_id=area_id,
        opportunity_id=opportunity_id,
        limit=limit,
    )
