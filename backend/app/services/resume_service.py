"""Resume composition, Surgical AI fine-tuning, HTML preview, and `auto_fit` A4 PDF export."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import Field
from sqlmodel import Session, col, select

from app.adapters.llm.pii_stripper import strip_pii_for_surgical_llm
from app.adapters.llm.providers import ProviderName, build_llm_adapter
from app.adapters.pdf.ats_validator import validate_ats_pdf
from app.adapters.pdf.html_renderer import render_resume_a4_html
from app.core.config import AppSettings, assert_safe_path
from app.domain.resume_composer import DeterministicResumeComposer, ResumeDocumentPayload
from app.domain.triage_engine import DeterministicTriageEngine, RawJobInput, TriageResult
from app.domain.veracity_guard import VeracityGuard
from app.models import (
    ApplicationEvent,
    ApplicationTracking,
    BlockedCompaniesConfig,
    JobOpportunity,
    ResumeVersion,
    SectionKind,
)
from app.models.profile_schema import SELF_DEPRECATING_PATTERN, MasterProfile, StrictModel
from app.services.discovery_service import load_domain_configs


class PdfRendererProtocol(Protocol):
    async def render_html_to_pdf(self, html_content: str, timeout_ms: int = 10_000) -> bytes:
        ...


class ResumeComposeRequest(StrictModel):
    area_id: str = Field(min_length=2, max_length=60)
    opportunity_id: str | None = None
    job_title: str | None = Field(default=None, max_length=250)
    company: str | None = Field(default=None, max_length=200)
    job_description: str = Field(default="", max_length=100000)
    section_order_override: list[SectionKind] | None = None
    standalone_preview: bool = False
    auto_fit: bool = True
    generation_mode: Literal["deterministic", "surgical_ai"] = "deterministic"
    payload_override: ResumeDocumentPayload | None = None


class SurgicalAdaptRequest(StrictModel):
    area_id: str = Field(min_length=2, max_length=60)
    opportunity_id: str | None = None
    job_title: str | None = Field(default=None, max_length=250)
    company: str | None = Field(default=None, max_length=200)
    job_description: str = Field(default="", max_length=100000)
    section_order_override: list[SectionKind] | None = None
    provider_override: ProviderName | None = None
    extra_job_keywords: list[str] = Field(default_factory=list)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_triage_context(
    session: Session,
    settings: AppSettings,
    area_id: str,
    opportunity_id: str | None,
    job_title: str | None,
    company: str | None,
    job_description: str,
) -> tuple[MasterProfile, BlockedCompaniesConfig, TriageResult | None, str, JobOpportunity | None]:
    profile, criteria, blocked, _ = load_domain_configs(settings)
    if area_id not in profile.area_presets:
        raise ValueError(f"Unknown area_id '{area_id}' in MasterProfile.area_presets")

    opp: JobOpportunity | None = None
    job_desc = job_description.strip()
    triage: TriageResult | None = None

    if opportunity_id:
        opp = session.get(JobOpportunity, opportunity_id)
        if opp is None:
            raise LookupError(f"Job opportunity '{opportunity_id}' not found.")
        if not job_desc:
            job_desc = opp.description_text or ""

    if job_desc or opp is not None:
        engine = DeterministicTriageEngine(criteria=criteria, blocked=blocked, profile=profile)
        raw_input = RawJobInput(
            title=job_title or (opp.title if opp else "Vaga Analisada"),
            company=company or (opp.company if opp else None),
            url=(opp.original_url if opp else "https://local.example.com/preview"),
            description=job_desc,
            location=(opp.location if opp else None),
            workplace_mode=(
                opp.workplace_mode
                if opp and opp.workplace_mode in {"presencial", "hibrido", "remoto"}
                else None
            ),  # type: ignore[arg-type]
        )
        triage = engine.evaluate(raw_input)

    return profile, blocked, triage, job_desc, opp


def _collect_forbidden_pdf_terms(profile: MasterProfile) -> list[str]:
    resumable_names = {
        s.display_name.casefold() for s in profile.skills if s.can_appear_on_resume
    }
    forbidden: list[str] = list(profile.forbidden_resume_terms)
    for proj in profile.projects:
        if proj.status == "reserved":
            forbidden.append(proj.name)
    for sk in profile.skills:
        if not sk.can_appear_on_resume and sk.display_name.casefold() not in resumable_names:
            if len(sk.display_name) >= 3:
                forbidden.append(sk.display_name)
    return forbidden


def _validate_payload_override(payload: ResumeDocumentPayload, profile: MasterProfile) -> None:
    if SELF_DEPRECATING_PATTERN.search(payload.summary.text):
        raise ValueError("Self-deprecating qualifier detected in payload_override.summary.")
    for exp in payload.experiences:
        for b in exp.bullets:
            if SELF_DEPRECATING_PATTERN.search(b.text):
                raise ValueError(f"Self-deprecating qualifier detected in bullet '{b.id}'.")
    forbidden_terms = _collect_forbidden_pdf_terms(profile)
    composer = DeterministicResumeComposer(profile)
    composer._assert_no_forbidden_leaks(payload, forbidden_terms)


def compose_resume_payload(
    session: Session,
    settings: AppSettings,
    req: ResumeComposeRequest,
) -> tuple[ResumeDocumentPayload, TriageResult | None, JobOpportunity | None, MasterProfile, str]:
    profile, _, triage, job_desc, opp = _resolve_triage_context(
        session=session,
        settings=settings,
        area_id=req.area_id,
        opportunity_id=req.opportunity_id,
        job_title=req.job_title,
        company=req.company,
        job_description=req.job_description,
    )
    if req.payload_override is not None:
        _validate_payload_override(req.payload_override, profile)
        return req.payload_override, triage, opp, profile, job_desc

    composer = DeterministicResumeComposer(profile)
    payload = composer.compose(
        req.area_id,
        triage=triage,
        job_description=job_desc,
        section_order_override=req.section_order_override,
    )
    return payload, triage, opp, profile, job_desc


async def surgical_adapt_resume(
    session: Session,
    settings: AppSettings,
    req: SurgicalAdaptRequest,
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    profile, blocked, triage, job_desc, _ = _resolve_triage_context(
        session=session,
        settings=settings,
        area_id=req.area_id,
        opportunity_id=req.opportunity_id,
        job_title=req.job_title,
        company=req.company,
        job_description=req.job_description,
    )
    if triage is not None and triage.hard_incompatibilities:
        raise ValueError(
            "Cannot adapt resume for incompatible opportunity: "
            + "; ".join(triage.hard_incompatibilities)
        )

    composer = DeterministicResumeComposer(profile)
    deterministic_payload = composer.compose(
        req.area_id,
        triage=triage,
        job_description=job_desc,
        section_order_override=req.section_order_override,
    )

    # 1. Strip PII & mask institutions/employers into minimal single-call payload
    strip_ctx = strip_pii_for_surgical_llm(
        payload=deterministic_payload,
        profile=profile,
        blocked=blocked,
        triage=triage,
        extra_job_keywords=req.extra_job_keywords,
    )

    # 2. Single-shot LLM call via selected adapter
    adapter = build_llm_adapter(
        settings=settings,
        provider_override=req.provider_override,
        transport=llm_transport,
    )
    llm_res = await adapter.adapt_once(strip_ctx.sanitized)

    # 3. Post-LLM VeracityGuard block-by-block validation + granular fallback + diff
    guard = VeracityGuard(profile=profile, triage=triage)
    guard_result = guard.verify_and_apply(
        deterministic_payload=deterministic_payload,
        proposal=llm_res.proposal,
        placeholder_to_original=strip_ctx.placeholder_to_original,
    )

    return {
        "provider": llm_res.provider,
        "model": llm_res.model,
        "token_usage": llm_res.token_usage.model_dump(mode="json"),
        "guard_warnings": [w.model_dump(mode="json") for w in guard_result.guard_warnings],
        "diff": [d.model_dump(mode="json") for d in guard_result.diff],
        "payload": guard_result.adapted_payload.model_dump(mode="json"),
        "triage": triage.model_dump(mode="json") if triage else None,
    }


async def export_validated_pdf(
    session: Session,
    settings: AppSettings,
    pdf_renderer: PdfRendererProtocol,
    req: ResumeComposeRequest,
) -> dict[str, Any]:
    profile, _, triage, job_desc, opp = _resolve_triage_context(
        session=session,
        settings=settings,
        area_id=req.area_id,
        opportunity_id=req.opportunity_id,
        job_title=req.job_title,
        company=req.company,
        job_description=req.job_description,
    )
    if triage is not None and triage.hard_incompatibilities:
        raise ValueError(
            "Cannot export resume for incompatible opportunity: "
            + "; ".join(triage.hard_incompatibilities)
        )

    composer = DeterministicResumeComposer(profile)
    forbidden_terms = _collect_forbidden_pdf_terms(profile)

    if req.payload_override is not None:
        _validate_payload_override(req.payload_override, profile)
        payload = req.payload_override.model_copy(deep=True)
    else:
        payload = composer.compose(
            req.area_id,
            triage=triage,
            job_description=job_desc,
            section_order_override=req.section_order_override,
        )

    html = render_resume_a4_html(payload, standalone_preview=False)
    pdf_bytes = await pdf_renderer.render_html_to_pdf(html)
    validation = validate_ats_pdf(pdf_bytes, payload, forbidden_terms)

    # Auto-fit controller when page_count overflows max_pages
    if req.auto_fit and validation.page_count > payload.max_pages:
        if req.payload_override is not None:
            payload.compact_mode = True
            payload.auto_fit_adjustments.append("Enabled compact A4 typography spacing")
            html = render_resume_a4_html(payload, standalone_preview=False)
            pdf_bytes = await pdf_renderer.render_html_to_pdf(html)
            validation = validate_ats_pdf(pdf_bytes, payload, forbidden_terms)
        else:
            adjustments: list[str] = []
            current_bullets = max((len(e.bullets) for e in payload.experiences), default=4)
            current_groups = len(payload.skill_groups)

            steps: list[tuple[bool, int, int, str]] = [
                (True, current_bullets, current_groups, "Enabled compact A4 typography spacing"),
            ]
            for b_lim in range(current_bullets - 1, 2, -1):
                steps.append((True, b_lim, current_groups, f"Reduced experience bullets to top {b_lim}"))
            for g_lim in range(current_groups - 1, 2, -1):
                steps.append((True, min(current_bullets, 3), g_lim, f"Reduced skill groups to top {g_lim}"))

            for compact_flag, b_lim, g_lim, note in steps:
                adjustments.append(note)
                payload = composer.compose(
                    req.area_id,
                    triage=triage,
                    job_description=job_desc,
                    section_order_override=req.section_order_override,
                    max_bullets_override=b_lim,
                    max_skill_groups_override=g_lim,
                    compact_mode=compact_flag,
                    auto_fit_notes=adjustments,
                )
                html = render_resume_a4_html(payload, standalone_preview=False)
                pdf_bytes = await pdf_renderer.render_html_to_pdf(html)
                validation = validate_ats_pdf(pdf_bytes, payload, forbidden_terms)
                if validation.page_count <= payload.max_pages:
                    break

    if not validation.passed:
        failed_keys = [k for k, ok in validation.checks.items() if not ok]
        raise ValueError(
            f"ATS PDF validation failed ({', '.join(failed_keys)}): "
            f"pages={validation.page_count}/{payload.max_pages}, "
            f"missing_links={validation.missing_links}, forbidden_hits={validation.forbidden_hits}"
        )

    target_dir = assert_safe_path(settings.exports_dir / payload.output_folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_pdf = assert_safe_path(target_dir / payload.output_pdf_filename)

    previous_pdf_sha = (
        hashlib.sha256(target_pdf.read_bytes()).hexdigest() if target_pdf.is_file() else None
    )
    new_pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

    fd, tmp_name = tempfile.mkstemp(prefix=".export-", suffix=".pdf", dir=str(target_dir))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pdf_bytes)
        os.replace(tmp_name, target_pdf)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    export_rel = target_pdf.relative_to(settings.safe_data_dir).as_posix()
    desc_sha = hashlib.sha256(job_desc.encode("utf-8")).hexdigest()
    now_iso = _utc_now()
    version_id = str(uuid4())

    changes_meta = {
        "ats_validation": validation.model_dump(mode="json"),
        "applied_equivalences": [eq.model_dump(mode="json") for eq in payload.applied_equivalences],
        "auto_fit_adjustments": payload.auto_fit_adjustments,
    }

    version_row = ResumeVersion(
        id=version_id,
        created_at=now_iso,
        opportunity_id=opp.id if opp else None,
        company=req.company or (opp.company if opp else None),
        job_title=req.job_title or (opp.title if opp else f"Preset Geral — {payload.display_name}"),
        source_url=opp.original_url if opp else None,
        area_id=payload.area_id,
        generation_mode=req.generation_mode,
        status="preparada_nao_enviada",
        description_sha256=desc_sha,
        pdf_sha256=new_pdf_sha,
        previous_pdf_sha256=previous_pdf_sha,
        export_rel_path=export_rel,
        rendered_payload_json=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False),
        changes_json=json.dumps(changes_meta, ensure_ascii=False),
    )
    session.add(version_row)
    session.flush()

    if opp is not None:
        if opp.application_stage in {"descoberta", "candidatura_a_preparar"}:
            opp.application_stage = "curriculo_preparado"
            session.add(opp)
        tracking = session.get(ApplicationTracking, opp.id)
        if tracking is None:
            session.add(
                ApplicationTracking(
                    opportunity_id=opp.id,
                    updated_at=now_iso,
                    stage="curriculo_preparado",
                    queue_state="candidatura_a_preparar",
                    resume_version_id=version_id,
                    notes=f"Exported validated A4 PDF ({req.generation_mode})",
                )
            )
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
                notes=f"Exported validated A4 PDF version {version_id} ({req.generation_mode}, SHA-256 {new_pdf_sha[:12]})",
            )
        )

    session.commit()

    return {
        "version_id": version_id,
        "area_id": payload.area_id,
        "locale": payload.locale,
        "generation_mode": req.generation_mode,
        "export_rel_path": export_rel,
        "output_pdf_filename": payload.output_pdf_filename,
        "pdf_sha256": new_pdf_sha,
        "pdf_size_bytes": len(pdf_bytes),
        "ats_validation": validation.model_dump(mode="json"),
        "payload": payload.model_dump(mode="json"),
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
