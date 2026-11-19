"""100% AI Resume Generation (`ResumeDraft`), offline HTML5 preview, and `auto_fit` A4 PDF export."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

import httpx
from pydantic import Field, field_validator
from sqlmodel import Session, col, select

from app.adapters.llm.pii_stripper import (
    rehydrate_generated_draft,
    strip_pii_for_resume_generation,
)
from app.adapters.llm.providers import build_llm_adapter
from app.adapters.pdf.ats_validator import validate_ats_pdf
from app.adapters.pdf.html_renderer import render_resume_a4_html
from app.core.config import AppSettings, assert_safe_path
from app.domain.triage_engine import normalize_text
from app.models import (
    ApplicationEvent,
    ApplicationTracking,
    BlockedCompaniesConfig,
    JobOpportunity,
    MasterProfile,
    ResumeDraft,
    ResumeFontFamily,
    ResumeVersion,
)
from app.models.profile_schema import StrictModel
from app.services.discovery_service import AssistedJobImportRequest, import_assisted_job, load_domain_configs


class PdfRendererProtocol(Protocol):
    async def render_html_to_pdf(self, html_content: str, timeout_ms: int = 10_000) -> bytes:
        ...


class ResumeGenerateRequest(StrictModel):
    opportunity_id: str | None = None
    job_title: str = Field(default="", max_length=250)
    company: str | None = Field(default=None, max_length=200)
    job_description: str = Field(default="", max_length=100000)
    max_pages: int = Field(default=1, ge=1, le=2)
    font_family: ResumeFontFamily = "Libre Franklin"
    profile_override: MasterProfile | None = None

    @field_validator("font_family", mode="before")
    @classmethod
    def migrate_legacy_font(cls, value: object) -> object:
        if value == "Merriweather":
            return "Times New Roman"
        return value


class ResumeRenderHtmlRequest(StrictModel):
    draft: ResumeDraft
    standalone_preview: bool = True


class ResumeExportPdfRequest(StrictModel):
    draft: ResumeDraft
    opportunity_id: str | None = None
    auto_fit: bool = True
    manual_job_description: str = Field(default="", max_length=100000)
    manual_job_url: str = Field(default="", max_length=1000)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ascii_slug(value: str | None, fallback: str, max_len: int = 45, sep: str = "_") -> str:
    decomposed = unicodedata.normalize("NFD", value or "")
    ascii_str = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = re.findall(r"[A-Za-z0-9]+", ascii_str)
    joined = sep.join(tokens)[:max_len].strip(sep)
    return joined or fallback


def build_internal_export_filename(
    version_id: str,
    company: str | None,
    target_role: str | None,
) -> str:
    """Immutable internal filename preventing overwrites across applications (`{vid8}_{empresa}_{cargo}.pdf`)."""
    vid8 = re.sub(r"[^a-zA-Z0-9]", "", version_id)[:8] or uuid4().hex[:8]
    comp_slug = _ascii_slug(company, "geral", max_len=32, sep="-").lower()
    role_slug = _ascii_slug(target_role, "curriculo", max_len=40, sep="-").lower()
    return f"{vid8}_{comp_slug}_{role_slug}.pdf"


def build_user_download_filename(full_name: str | None, target_role: str | None) -> str:
    """Clean portal-safe download filename (`{PrimeiroNome}_{UltimoNome}_{Cargo}.pdf`)."""
    decomposed_name = unicodedata.normalize("NFD", full_name or "")
    ascii_name = "".join(ch for ch in decomposed_name if not unicodedata.combining(ch))
    name_parts = [p.capitalize() for p in re.findall(r"[A-Za-z0-9]+", ascii_name)]
    if len(name_parts) >= 2:
        first_last = f"{name_parts[0]}_{name_parts[-1]}"
    elif len(name_parts) == 1:
        first_last = name_parts[0]
    else:
        first_last = "Candidato"

    role_clean = _ascii_slug(target_role, "Curriculo", max_len=48, sep="_")
    return f"{first_last}_{role_clean}.pdf"


def _is_company_blocked(company_name: str | None, blocked: BlockedCompaniesConfig) -> bool:
    if not company_name or not company_name.strip():
        return False
    norm_target = normalize_text(company_name)
    for blk in blocked.companies:
        candidates = [blk.display_name, blk.corporate_name or "", *blk.aliases]
        for cand in candidates:
            norm_cand = normalize_text(cand)
            if norm_cand and (norm_cand == norm_target or norm_cand in norm_target):
                return True
    return False


async def generate_ai_resume_draft(
    session: Session,
    settings: AppSettings,
    req: ResumeGenerateRequest,
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    saved_profile, _, blocked, _ = load_domain_configs(settings)
    profile = req.profile_override or saved_profile

    opp: JobOpportunity | None = None
    job_title = req.job_title.strip()
    company = (req.company or "").strip() or None
    job_desc = req.job_description.strip()

    if req.opportunity_id:
        opp = session.get(JobOpportunity, req.opportunity_id)
        if opp is None:
            raise LookupError(f"Vaga '{req.opportunity_id}' não encontrada.")
        if not job_title:
            job_title = opp.title
        if not company:
            company = opp.company
        if not job_desc:
            job_desc = opp.description_text or ""

    if not job_desc and not job_title:
        raise ValueError("Informe a descrição ou o título da vaga para gerar o currículo personalizado.")

    if _is_company_blocked(company, blocked):
        raise ValueError("Esta empresa consta na sua lista de empresas bloqueadas.")

    strip_ctx = strip_pii_for_resume_generation(
        profile=profile,
        blocked=blocked,
        job_title=job_title,
        company=company,
        job_description=job_desc,
        max_pages=req.max_pages,
    )

    adapter = build_llm_adapter(
        settings=settings,
        transport=llm_transport,
    )
    llm_res = await adapter.generate_tailored_resume(strip_ctx.sanitized)

    draft = rehydrate_generated_draft(
        proposal_dict=llm_res.data,
        profile=profile,
        max_pages=req.max_pages,
        company=company,
        placeholder_to_original=strip_ctx.placeholder_to_original,
    )
    updates: dict[str, Any] = {"font_family": req.font_family}
    if not draft.target_role and job_title:
        updates["target_role"] = job_title[:200]
    draft = draft.model_copy(update=updates)

    return {
        "provider": llm_res.provider,
        "model": llm_res.model,
        "token_usage": llm_res.token_usage.model_dump(mode="json"),
        "job_context": {
            "opportunity_id": opp.id if opp else None,
            "job_title": job_title,
            "company": company,
        },
        "draft": draft.model_dump(mode="json"),
    }


def _shrink_draft_for_budget(draft: ResumeDraft, attempt: int) -> ResumeDraft:
    if attempt == 1:
        return draft.model_copy(update={"compact_mode": True})
    if attempt == 2:
        trimmed_exps = [
            exp.model_copy(update={"highlights": exp.highlights[:4]})
            for exp in draft.experiences
        ]
        return draft.model_copy(update={"compact_mode": True, "experiences": trimmed_exps})
    return draft.model_copy(
        update={
            "compact_mode": True,
            "courses": draft.courses[:2],
            "experiences": [
                exp.model_copy(update={"highlights": exp.highlights[:3]})
                for exp in draft.experiences
            ],
        }
    )


async def export_validated_pdf(
    session: Session,
    settings: AppSettings,
    pdf_renderer: PdfRendererProtocol,
    req: ResumeExportPdfRequest,
) -> dict[str, Any]:
    opp: JobOpportunity | None = None
    if req.opportunity_id:
        opp = session.get(JobOpportunity, req.opportunity_id)
        if opp is None:
            raise LookupError(f"Vaga '{req.opportunity_id}' não encontrada.")

    current_draft = req.draft
    max_attempts = 4 if req.auto_fit else 1
    pdf_bytes = b""
    validation = None

    for attempt in range(max_attempts):
        if attempt > 0:
            current_draft = _shrink_draft_for_budget(current_draft, attempt)
        html = render_resume_a4_html(current_draft, standalone_preview=False)
        pdf_bytes = await pdf_renderer.render_html_to_pdf(html)
        validation = validate_ats_pdf(pdf_bytes, current_draft)
        if validation.page_count <= current_draft.max_pages:
            break

    assert validation is not None
    if not validation.passed:
        failed = [k for k, ok in validation.checks.items() if not ok]
        raise ValueError(
            f"Falha na validação ATS do PDF ({failed}): páginas={validation.page_count}/{validation.max_pages}"
        )

    if opp is None and req.manual_job_description.strip():
        imported = import_assisted_job(
            session,
            settings,
            AssistedJobImportRequest(
                url=req.manual_job_url,
                title=current_draft.target_role,
                company=current_draft.company,
                description=req.manual_job_description,
            ),
        )
        opp = session.get(JobOpportunity, imported["opportunity_id"])

    now_iso = _utc_now()
    version_id = str(uuid4())
    effective_company = opp.company if opp else current_draft.company
    effective_role = current_draft.target_role or (opp.title if opp else None)

    internal_filename = build_internal_export_filename(version_id, effective_company, effective_role)
    download_filename = build_user_download_filename(current_draft.header.full_name, effective_role)

    target_dir = assert_safe_path(settings.exports_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_pdf_path = assert_safe_path(target_dir / internal_filename)

    prev_sha: str | None = None
    if target_pdf_path.is_file():
        prev_sha = hashlib.sha256(target_pdf_path.read_bytes()).hexdigest()

    fd, tmp_name = tempfile.mkstemp(prefix=".resumatch_", suffix=".pdf", dir=str(target_dir))
    tmp_path_str = os.fspath(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pdf_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path_str, target_pdf_path)
    finally:
        if os.path.exists(tmp_path_str):
            try:
                os.remove(tmp_path_str)
            except OSError:
                pass

    new_pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    draft_json = json.dumps(current_draft.model_dump(mode="json"), ensure_ascii=False)
    desc_sha = hashlib.sha256(draft_json.encode("utf-8")).hexdigest()
    export_rel = target_pdf_path.relative_to(settings.safe_data_dir).as_posix()

    area_tag = (opp.area_id if opp and opp.area_id else None) or "geral"

    version = ResumeVersion(
        id=version_id,
        created_at=now_iso,
        opportunity_id=opp.id if opp else None,
        company=effective_company,
        job_title=(opp.title if opp else current_draft.target_role or None),
        source_url=(opp.original_url if opp else None),
        area_id=area_tag,
        generation_mode="ai",
        status="exported",
        description_sha256=desc_sha,
        pdf_sha256=new_pdf_sha,
        previous_pdf_sha256=prev_sha,
        export_rel_path=export_rel,
        rendered_payload_json=draft_json,
        changes_json=json.dumps(
            {
                "output_pdf_filename": download_filename,
                "ats_validation": validation.model_dump(mode="json"),
            },
            ensure_ascii=False,
        ),
    )
    session.add(version)
    session.flush()

    if opp is not None:
        if opp.application_stage in {"descoberta", "candidatura_a_preparar"}:
            opp.application_stage = "curriculo_preparado"
            session.add(opp)

        tracking = session.get(ApplicationTracking, opp.id)
        if tracking is None:
            tracking = ApplicationTracking(
                opportunity_id=opp.id,
                updated_at=now_iso,
                stage="curriculo_preparado",
                queue_state="curriculo_preparado",
                resume_version_id=version_id,
                service_tier=opp.service_tier,
                automation_capability=opp.automation_capability,
                application_method=opp.application_method,
            )
            session.add(tracking)
        else:
            tracking.updated_at = now_iso
            tracking.resume_version_id = version_id
            if tracking.stage in {"descoberta", "candidatura_a_preparar"}:
                tracking.stage = "curriculo_preparado"
            session.add(tracking)

        session.add(
            ApplicationEvent(
                opportunity_id=opp.id,
                created_at=now_iso,
                event_source="tracking",
                stage="curriculo_preparado",
                notes=f"Currículo PDF A4 exportado ({version_id[:8]}, SHA-256 {new_pdf_sha[:12]})",
            )
        )

    session.commit()

    return {
        "version_id": version_id,
        "opportunity_id": opp.id if opp else None,
        "job_url": opp.original_url if opp else None,
        "generation_mode": "ai",
        "export_rel_path": export_rel,
        "output_pdf_filename": download_filename,
        "pdf_sha256": new_pdf_sha,
        "pdf_size_bytes": len(pdf_bytes),
        "ats_validation": validation.model_dump(mode="json"),
        "draft": current_draft.model_dump(mode="json"),
    }


def list_resume_versions(
    session: Session,
    *,
    area_id: str | None = None,
    opportunity_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    stmt = select(ResumeVersion)
    if area_id:
        stmt = stmt.where(ResumeVersion.area_id == area_id)
    if opportunity_id:
        stmt = stmt.where(ResumeVersion.opportunity_id == opportunity_id)
    stmt = stmt.order_by(col(ResumeVersion.created_at).desc()).limit(limit)
    rows = session.exec(stmt).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "opportunity_id": r.opportunity_id,
            "company": r.company,
            "job_title": r.job_title,
            "area_id": r.area_id,
            "generation_mode": r.generation_mode,
            "status": r.status,
            "pdf_sha256": r.pdf_sha256,
            "export_rel_path": r.export_rel_path,
            "changes": (
                json.loads(r.changes_json or "{}")
                if (r.changes_json or "").startswith("{")
                else json.loads(r.changes_json or "[]")
            ),
        }
        for r in rows
    ]
