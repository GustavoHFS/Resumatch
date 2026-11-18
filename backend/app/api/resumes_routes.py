"""REST endpoints for AI resume generation (`POST /api/resumes/generate`), HTML preview, static fonts, and PDF export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlmodel import Session

from app.adapters.llm.providers import LlmAdapterError
from app.adapters.pdf.browser_manager import ChromiumPdfBrowserManager, PdfRenderError
from app.adapters.pdf.html_renderer import ALLOWED_FONT_FILES, FONTS_DIR, render_resume_a4_html
from app.api.routes import get_app_settings
from app.core.config import AppSettings, assert_safe_path
from app.core.database import get_db_session
from app.models import ResumeVersion
from app.services.resume_service import (
    ResumeExportPdfRequest,
    ResumeGenerateRequest,
    ResumeRenderHtmlRequest,
    build_user_download_filename,
    export_validated_pdf,
    generate_ai_resume_draft,
    list_resume_versions,
)

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


def get_pdf_browser_manager(request: Request) -> ChromiumPdfBrowserManager:
    return request.app.state.pdf_browser_manager


@router.get("/fonts/{font_filename}", include_in_schema=False)
def serve_preview_woff2_font(font_filename: str) -> FileResponse:
    if font_filename not in ALLOWED_FONT_FILES:
        raise HTTPException(status_code=404, detail="Font asset not found.")
    font_path = (FONTS_DIR / font_filename).resolve()
    if not font_path.is_file():
        raise HTTPException(status_code=404, detail="Font file missing.")
    return FileResponse(
        path=font_path,
        media_type="font/woff2",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/generate")
async def generate_resume_endpoint(
    request: Request,
    payload: ResumeGenerateRequest,
    session: Session = Depends(get_db_session),
    settings: AppSettings = Depends(get_app_settings),
) -> dict[str, Any]:
    llm_transport = getattr(request.app.state, "llm_transport", None)
    try:
        return await generate_ai_resume_draft(
            session=session,
            settings=settings,
            req=payload,
            llm_transport=llm_transport,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LlmAdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/render-html")
def render_resume_html_endpoint(
    payload: ResumeRenderHtmlRequest,
) -> dict[str, Any]:
    html = render_resume_a4_html(payload.draft, standalone_preview=payload.standalone_preview)
    return {
        "max_pages": payload.draft.max_pages,
        "html": html,
        "draft": payload.draft.model_dump(mode="json"),
    }


@router.post("/export-pdf")
async def export_resume_pdf_endpoint(
    payload: ResumeExportPdfRequest,
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


@router.get("/versions/{version_id}/pdf")
def download_resume_version_pdf(
    version_id: str,
    session: Session = Depends(get_db_session),
    settings: AppSettings = Depends(get_app_settings),
) -> FileResponse:
    version = session.get(ResumeVersion, version_id)
    if version is None or not version.export_rel_path:
        raise HTTPException(status_code=404, detail="PDF da versão não encontrado.")

    base_dir = settings.safe_data_dir.resolve()
    candidate_path = assert_safe_path(base_dir / Path(version.export_rel_path))
    if base_dir not in candidate_path.parents or not candidate_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo PDF exportado ausente em disco.")

    user_filename: str | None = None
    try:
        changes = json.loads(version.changes_json or "{}")
        if isinstance(changes, dict) and isinstance(changes.get("output_pdf_filename"), str):
            user_filename = changes["output_pdf_filename"]
        if not user_filename and version.rendered_payload_json:
            draft_obj = json.loads(version.rendered_payload_json)
            full_name = ((draft_obj.get("header") or {}).get("full_name") if isinstance(draft_obj, dict) else None)
            role = (draft_obj.get("target_role") if isinstance(draft_obj, dict) else None) or version.job_title
            if full_name:
                user_filename = build_user_download_filename(str(full_name), str(role or ""))
    except ValueError:
        pass

    return FileResponse(
        path=candidate_path,
        media_type="application/pdf",
        filename=user_filename or candidate_path.name,
    )
