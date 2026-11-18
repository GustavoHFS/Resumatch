"""Phase 3 test suite: Editable `ResumeDraft` -> Offline HTML5/CSS A4 rendering (`pt-BR` & `en-US`),
Playwright Headless Chromium lazy reuse + idle timeout, `auto_fit` page budget, and `pypdf` ATS QA.

Strictly uses fictional fixtures from `examples/` and `httpx.ASGITransport` (zero background servers).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.adapters.pdf.browser_manager import ChromiumPdfBrowserManager
from app.core.config import AppSettings
from app.main import create_app
from app.models import (
    DraftCourseItem,
    DraftEducationItem,
    DraftExperienceItem,
    DraftLanguageItem,
    DraftSkillGroup,
    MasterProfile,
    ResumeDraft,
    build_draft_header_from_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _build_sample_draft(
    profile: MasterProfile,
    max_pages: int = 1,
) -> ResumeDraft:
    return ResumeDraft(
        target_role="Desenvolvedora Backend Java",
        company="Minas Software",
        max_pages=max_pages,
        header=build_draft_header_from_profile(profile.header),
        summary="Desenvolvedora focada em Java, Spring Boot, APIs REST e bancos relacionais SQL.",
        experiences=[
            DraftExperienceItem(
                id="exp_1",
                role="Técnica de Suporte de TI",
                company="Horizonte Tech Solutions",
                period="2024",
                location="Belo Horizonte, MG",
                highlights=[
                    "Atendimento Help Desk presencial e remoto com triagem de chamados.",
                    "Administração de usuários e permissões no Active Directory.",
                ],
            )
        ],
        education=[
            DraftEducationItem(
                id="edu_1",
                degree=profile.education[0].degree,
                institution=profile.education[0].institution,
                period=profile.education[0].period,
                details=profile.education[0].details,
            )
        ],
        courses=[
            DraftCourseItem(
                id="crs_1",
                title=profile.courses[0].title,
                institution=profile.courses[0].institution,
                workload=profile.courses[0].workload,
                completion_date=profile.courses[0].completion_date,
                description=profile.courses[0].description,
            )
        ],
        skill_groups=[
            DraftSkillGroup(
                id="sk_1",
                label="Desenvolvimento Backend",
                items=["Java", "Spring Boot", "APIs REST", "SQL"],
            )
        ],
        languages=[
            DraftLanguageItem(id="l1", language="Português", proficiency="Nativo")
        ],
    )


@asynccontextmanager
async def _asgi_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


class TestPlaywrightPdfLifecycleAutoFitAndDraftExport:
    @pytest.mark.anyio
    async def test_browser_lazy_reuse_offline_guard_and_idle_timeout_autoclose(self) -> None:
        manager = ChromiumPdfBrowserManager(idle_timeout_seconds=0.3)
        try:
            assert manager.is_browser_running is False
            assert manager.launch_count == 0

            html_with_external = (
                "<!DOCTYPE html><html><body>"
                "<h1>Offline Render Check</h1>"
                '<img src="https://external-tracker.example.org/pixel.png" alt="blocked">'
                "</body></html>"
            )
            pdf_1 = await manager.render_html_to_pdf(html_with_external)
            assert pdf_1.startswith(b"%PDF-")
            assert manager.is_browser_running is True
            assert manager.launch_count == 1
            assert manager.render_count == 1

            pdf_2 = await manager.render_html_to_pdf(
                "<!DOCTYPE html><html><body><p>Fast warm render</p></body></html>"
            )
            assert pdf_2.startswith(b"%PDF-")
            assert manager.launch_count == 1
            assert manager.render_count == 2

            await asyncio.sleep(0.55)
            assert manager.is_browser_running is False
            assert manager.idle_close_count == 1
        finally:
            await manager.aclose()

    @pytest.mark.anyio
    async def test_render_html_export_pdf_and_autofit_budget_from_resume_draft(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "phase3_v2_ws"
        settings = AppSettings(
            data_dir=workspace,
            examples_dir=EXAMPLES_DIR,
            scheduler_enabled=False,
            pdf_browser_idle_timeout_seconds=30,
        )
        profile = MasterProfile.model_validate(
            json.loads((EXAMPLES_DIR / "master_profile.example.json").read_text(encoding="utf-8"))
        )
        app = create_app(settings=settings)

        async with _asgi_client(app) as client:
            assert app.state.pdf_browser_manager.launch_count == 0

            # 1. Render HTML preview in Portuguese-BR (lightweight static font URL in standalone_preview=True)
            pt_draft = _build_sample_draft(profile)
            html_res = await client.post(
                "/api/resumes/render-html",
                json={"draft": pt_draft.model_dump(mode="json"), "standalone_preview": True},
            )
            assert html_res.status_code == 200
            html_str = html_res.json()["html"]
            assert '<html lang="pt-BR">' in html_str
            assert "/api/resumes/fonts/LibreFranklin-Regular.woff2" in html_str
            assert len(html_str) < 15_000
            assert "fonts.googleapis.com" not in html_str
            assert 'id="resume-flow"' in html_str
            assert "Desenvolvedora Backend Java" in html_str
            assert 'href="mailto:marina.costa@example.com"' in html_str
            assert "RESUMO PROFISSIONAL" in html_str
            assert "CURSOS E CERTIFICAÇÕES" in html_str

            # Offline PDF mode (standalone_preview=False) embeds Base64 WOFF2 directly
            offline_html = (
                await client.post(
                    "/api/resumes/render-html",
                    json={"draft": pt_draft.model_dump(mode="json"), "standalone_preview": False},
                )
            ).json()["html"]
            assert "data:font/woff2;base64," in offline_html

            # Static font endpoint serves embedded WOFF2 and blocks unknown filenames
            font_ok = await client.get("/api/resumes/fonts/Inter-Regular.woff2")
            assert font_ok.status_code == 200
            assert font_ok.headers["content-type"] == "font/woff2"
            assert font_ok.content[:4] == b"wOF2"
            assert (await client.get("/api/resumes/fonts/unknown.woff2")).status_code == 404

            # Verify font_family selector updates rendered CSS stack cleanly
            georgia_draft = pt_draft.model_copy(update={"font_family": "Georgia"})
            georgia_html = (
                await client.post(
                    "/api/resumes/render-html",
                    json={"draft": georgia_draft.model_dump(mode="json"), "standalone_preview": True},
                )
            ).json()["html"]
            assert "--resume-font-family: Georgia," in georgia_html

            # 2. Import a compatible job and export its tailored A4 PDF from ResumeDraft
            imp_res = await client.post(
                "/api/jobs/import",
                json={
                    "url": "https://jobs.example.com/vagas/dev-java-jr",
                    "title": "Estágio em Desenvolvimento Java",
                    "company": "Minas Software",
                    "location": "Belo Horizonte, MG",
                    "workplace_mode": "presencial",
                    "description": (
                        "Estágio remunerado com bolsa auxílio, 09h às 15h.\n"
                        "Requisitos: Java, Spring Boot, SQL e Git."
                    ),
                },
            )
            opp_id = imp_res.json()["opportunity_id"]

            exp_res = await client.post(
                "/api/resumes/export-pdf",
                json={
                    "draft": pt_draft.model_dump(mode="json"),
                    "opportunity_id": opp_id,
                    "auto_fit": True,
                },
            )
            assert exp_res.status_code == 200, exp_res.text
            exp_body = exp_res.json()
            ats = exp_body["ats_validation"]
            assert ats["passed"] is True
            assert ats["page_count"] == 1
            assert ats["zero_corrupted_ligatures"] is True
            assert ats["clickable_links_valid"] is True
            assert len(ats["clickable_uris"]) == 3
            assert "mailto:marina.costa@example.com" in ats["clickable_uris"]
            assert exp_body["output_pdf_filename"] == "Marina_Costa_Desenvolvedora_Backend_Java.pdf"
            pdf_1_path = workspace / exp_body["export_rel_path"]
            assert pdf_1_path.is_file()

            # Exporting same role for another company creates a distinct immutable PDF file
            exp_res_2 = await client.post(
                "/api/resumes/export-pdf",
                json={
                    "draft": pt_draft.model_copy(update={"company": "Outra Empresa Tech"}).model_dump(mode="json"),
                    "auto_fit": True,
                },
            )
            assert exp_res_2.status_code == 200, exp_res_2.text
            exp_body_2 = exp_res_2.json()
            assert exp_body_2["export_rel_path"] != exp_body["export_rel_path"]
            assert pdf_1_path.is_file()
            assert (workspace / exp_body_2["export_rel_path"]).is_file()

            job_detail = (await client.get(f"/api/jobs/{opp_id}")).json()
            assert job_detail["opportunity"]["application_stage"] == "curriculo_preparado"
            assert job_detail["opportunity"]["labels"]["stage_label"] == "Currículo pronto"
            assert job_detail["tracking"]["resume_version_id"] == exp_body["version_id"]

            # 3. Auto-Fit Page Overflow Test on verbose 1-page draft
            verbose_draft = pt_draft.model_copy(
                update={
                    "summary": pt_draft.summary * 4,
                    "experiences": [
                        pt_draft.experiences[0].model_copy(
                            update={
                                "highlights": [
                                    f"Entrega técnica detalhada #{i}: administração corporativa Windows, redes TCP/IP, atendimento estruturado de chamados e automação de rotinas."
                                    for i in range(1, 10)
                                ]
                            }
                        )
                    ],
                    "courses": [
                        DraftCourseItem(
                            id=f"c_{i}",
                            title=f"Formação Técnica Intensiva {i}",
                            institution="Instituto Aberto",
                            workload="60 horas",
                            completion_date="2026",
                            description="Aprofundamento prático em arquitetura backend e bancos relacionais.",
                        )
                        for i in range(1, 7)
                    ],
                }
            )
            autofit_res = await client.post(
                "/api/resumes/export-pdf",
                json={"draft": verbose_draft.model_dump(mode="json"), "auto_fit": True},
            )
            assert autofit_res.status_code == 200, autofit_res.text
            af_body = autofit_res.json()
            assert af_body["ats_validation"]["passed"] is True
            assert af_body["ats_validation"]["page_count"] == 1
            assert af_body["draft"]["compact_mode"] is True
            assert app.state.pdf_browser_manager.launch_count == 1

    @pytest.mark.anyio
    async def test_all_five_selector_fonts_export_clean_pdf_with_zero_ligatures(
        self, tmp_path: Path
    ) -> None:
        import io
        import re
        from pypdf import PdfReader

        workspace = tmp_path / "phase3_5fonts_ws"
        settings = AppSettings(
            data_dir=workspace,
            examples_dir=EXAMPLES_DIR,
            scheduler_enabled=False,
            pdf_browser_idle_timeout_seconds=30,
        )
        profile = MasterProfile.model_validate(
            json.loads((EXAMPLES_DIR / "master_profile.example.json").read_text(encoding="utf-8"))
        )
        ligature_draft = _build_sample_draft(profile).model_copy(
            update={
                "summary": (
                    "Profissional com perfil eficiente, fluxo contínuo, configuração, "
                    "identificação de incidentes, certificações técnicas e currículo focado em Java."
                )
            }
        )
        corrupted_re = re.compile(r"[\ufb00-\ufb06\u0131]")
        app = create_app(settings=settings)

        async with _asgi_client(app) as client:
            for family in [
                "Libre Franklin",
                "Inter",
                "Arial",
                "Georgia",
                "Times New Roman",
                "Merriweather",  # legacy alias migrated automatically to Times New Roman
            ]:
                payload_draft = ligature_draft.model_dump(mode="json")
                payload_draft["font_family"] = family
                res = await client.post(
                    "/api/resumes/export-pdf",
                    json={"draft": payload_draft, "auto_fit": True},
                )
                assert res.status_code == 200, f"Font '{family}' failed: {res.text}"
                body = res.json()
                assert body["ats_validation"]["passed"] is True
                assert body["ats_validation"]["zero_corrupted_ligatures"] is True
                assert body["ats_validation"]["corrupted_glyphs"] == []

                pdf_bytes = (workspace / body["export_rel_path"]).read_bytes()
                extracted = "\n".join(
                    (p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf_bytes)).pages
                )
                assert not corrupted_re.search(extracted), f"Corrupted glyph in '{family}'"
                assert "eficiente" in extracted
                assert "fluxo" in extracted
                assert "identificação" in extracted
                assert "Desenvolvedora Backend Java" in extracted
