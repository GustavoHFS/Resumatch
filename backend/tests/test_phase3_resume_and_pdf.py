"""Phase 3 test suite: DeterministicResumeComposer (<20ms), HTML5/CSS A4 rendering,
Playwright Headless Chromium lazy reuse + idle timeout, auto_fit page budget, and pypdf ATS QA.

Strictly uses fictional fixtures from `examples/` and `httpx.ASGITransport` (zero background servers).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.adapters.pdf.browser_manager import ChromiumPdfBrowserManager
from app.core.config import AppSettings
from app.domain.resume_composer import DeterministicResumeComposer
from app.domain.triage_engine import DeterministicTriageEngine, RawJobInput
from app.main import create_app
from app.models import (
    BlockedCompaniesConfig,
    MasterProfile,
    SearchCriteria,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _load_demo_domain() -> tuple[MasterProfile, SearchCriteria, BlockedCompaniesConfig]:
    profile = MasterProfile.model_validate(
        json.loads((EXAMPLES_DIR / "master_profile.example.json").read_text(encoding="utf-8"))
    )
    criteria = SearchCriteria.model_validate(
        json.loads((EXAMPLES_DIR / "search_criteria.example.json").read_text(encoding="utf-8"))
    )
    blocked = BlockedCompaniesConfig.model_validate(
        json.loads((EXAMPLES_DIR / "blocked_companies.example.json").read_text(encoding="utf-8"))
    )
    return profile, criteria, blocked


@asynccontextmanager
async def _asgi_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


class TestDeterministicResumeComposer:
    def test_composes_in_under_20ms_with_provenance_equivalences_and_veracity_guards(self) -> None:
        profile, criteria, blocked = _load_demo_domain()
        triage_engine = DeterministicTriageEngine(criteria, blocked, profile)
        composer = DeterministicResumeComposer(profile)

        job_desc = (
            "Estágio remunerado em Suporte Técnico - Belo Horizonte, 08h às 14h.\n"
            "Requisitos:\n"
            "- Atendimento Help Desk, Windows e Active Directory.\n"
            "- Configuração de correio eletrônico e cabeamento estruturado.\n"
            "- Conhecimento em GLPI e Docker.\n"
        )
        triage = triage_engine.evaluate(
            RawJobInput(
                title="Estágio em Suporte Técnico",
                company="Horizonte Clínicas",
                url="https://jobs.example.com/suporte-bh",
                location="Belo Horizonte, MG",
                workplace_mode="presencial",
                description=job_desc,
            )
        )

        t0 = time.perf_counter()
        payload = composer.compose(
            "suporte",
            triage=triage,
            job_description=job_desc,
            section_order_override=["summary", "experience", "education", "skills", "languages"],
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert elapsed_ms < 20.0
        assert payload.section_order == ["summary", "experience", "education", "skills", "languages"]
        # Contextual equivalences applied & recorded
        eq_to_terms = {eq.to_term for eq in payload.applied_equivalences}
        assert "correio eletrônico" in eq_to_terms
        assert "cabeamento estruturado" in eq_to_terms

        # Every bullet and skill group preserves factual_source
        assert all(b.factual_source for b in payload.experiences[0].bullets)
        assert all(g.factual_source for g in payload.skill_groups)

        # Strict blocks: in_progress_free course, reserved project, GLPI, Docker never appear!
        serialized = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)
        assert "GLPI" not in serialized
        assert "Docker" not in serialized
        assert "AtlasFlow Sandbox" not in serialized
        assert "Containers e DevOps" not in serialized

    def test_composes_english_preset_without_age_and_with_english_labels(self) -> None:
        profile, _, _ = _load_demo_domain()
        composer = DeterministicResumeComposer(profile)
        payload = composer.compose("suporte-en")

        assert payload.locale == "en-US"
        assert "21" not in payload.header.location_line
        assert "Brazil" in payload.header.location_line
        assert payload.section_labels.summary == "PROFESSIONAL SUMMARY"
        assert payload.languages[0].language == "English"
        assert payload.languages[0].proficiency == "Advanced"


class TestPlaywrightPdfLifecycleAutoFitAndRestEndpoints:
    @pytest.mark.anyio
    async def test_browser_lazy_reuse_offline_guard_and_idle_timeout_autoclose(self) -> None:
        manager = ChromiumPdfBrowserManager(idle_timeout_seconds=0.3)
        try:
            # 1. Lazy: browser must NOT be running before first render
            assert manager.is_browser_running is False
            assert manager.launch_count == 0

            # 2. Render #1 (includes blocked external image URL to verify offline route guard)
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

            # 3. Render #2 immediately reuses the SAME warm Chromium instance
            pdf_2 = await manager.render_html_to_pdf("<!DOCTYPE html><html><body><p>Fast warm render</p></body></html>")
            assert pdf_2.startswith(b"%PDF-")
            assert manager.launch_count == 1
            assert manager.render_count == 2

            # 4. Wait past idle_timeout_seconds=0.3 -> watchdog closes Chromium automatically!
            await asyncio.sleep(0.55)
            assert manager.is_browser_running is False
            assert manager.idle_close_count == 1
        finally:
            await manager.aclose()

    @pytest.mark.anyio
    async def test_rest_compose_render_html_export_pdf_and_autofit_overflow(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "phase3_ws"
        settings = AppSettings(
            data_dir=workspace,
            examples_dir=EXAMPLES_DIR,
            scheduler_enabled=False,
            pdf_browser_idle_timeout_seconds=30,
        )
        app = create_app(settings=settings)

        async with _asgi_client(app) as client:
            # Browser remains lazy until /export-pdf is called
            assert app.state.pdf_browser_manager.launch_count == 0

            # 1. Compose endpoint (< 20ms)
            comp_res = await client.post("/api/resumes/compose", json={"area_id": "dev"})
            assert comp_res.status_code == 200
            assert comp_res.json()["elapsed_ms"] < 50.0

            # 2. Render HTML preview endpoint (offline embedded @font-face WOFF2, zero external font links)
            html_res = await client.post(
                "/api/resumes/render-html",
                json={"area_id": "suporte-en", "standalone_preview": True},
            )
            assert html_res.status_code == 200
            html_str = html_res.json()["html"]
            assert "data:font/woff2;base64," in html_str
            assert "fonts.googleapis.com" not in html_str
            assert "PROFESSIONAL SUMMARY" in html_str

            # 3. Import a compatible job and export its tailored A4 PDF
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
                json={"area_id": "dev", "opportunity_id": opp_id},
            )
            assert exp_res.status_code == 200, exp_res.text
            exp_body = exp_res.json()
            ats = exp_body["ats_validation"]
            assert ats["passed"] is True
            assert ats["page_count"] == 1
            assert ats["clickable_links_valid"] is True
            assert len(ats["clickable_uris"]) == 2
            assert (workspace / exp_body["export_rel_path"]).is_file()

            # Verify job stage transitioned to 'curriculo_preparado' and version is listed
            job_detail = (await client.get(f"/api/jobs/{opp_id}")).json()
            assert job_detail["opportunity"]["application_stage"] == "curriculo_preparado"
            assert job_detail["tracking"]["resume_version_id"] == exp_body["version_id"]

            versions = (await client.get("/api/resumes/versions")).json()
            assert len(versions) == 1
            assert versions[0]["id"] == exp_body["version_id"]

            # 4. Auto-Fit Page Overflow Test: inject verbose bullets (<=400 chars) + groups that spill past 1 page A4
            prof_raw = json.loads(settings.profile_path.read_text(encoding="utf-8"))
            prof_raw["area_presets"]["suporte"]["default_summary"] = (
                "Profissional de TI com sólida atuação prática em suporte técnico presencial e remoto, "
                "administração de contas no Active Directory, gestão estruturada de chamados em Help Desk, "
                "manutenção preventiva e corretiva de estações Windows, configuração de redes corporativas, "
                "atendimento direto a clientes internos e externos, documentação de incidentes e organização "
                "de cotações técnicas em planilhas operacionais."
            )
            long_clause = (
                "Atuação detalhada em suporte corporativo, administração de estações Windows, "
                "redes TCP/IP, atendimento estruturado de chamados, documentação operacional, "
                "manutenção preventiva de equipamentos, impressoras corporativas e rotinas de backup diário."
            )
            extra_ids = []
            for idx in range(1, 9):
                bid = f"bul_overflow_{idx}"
                extra_ids.append(bid)
                prof_raw["experiences"][0]["bullets"].append(
                    {
                        "id": bid,
                        "text": f"Atividade operacional {idx}: {long_clause}",
                        "areas": ["suporte"],
                        "skill_tags": ["windows"],
                        "keywords": ["windows"],
                        "factual_source": "Overflow test fixture",
                        "translations": {},
                    }
                )
            for g_idx in range(1, 5):
                prof_raw["area_presets"]["suporte"]["skill_groups"].append(
                    {
                        "id": f"grp_extra_{g_idx}",
                        "label": f"Domínio Complementar {g_idx}",
                        "items_text": (
                            "Administração de ambientes Windows corporativos, redes cabeadas e Wi-Fi, "
                            "diagnóstico estruturado de conectividade DNS/DHCP, suporte remoto e documentação."
                        ),
                        "skill_ids": ["windows", "redes"],
                        "factual_source": "Overflow test fixture",
                    }
                )
            prof_raw["area_presets"]["suporte"]["max_bullets_per_experience"] = 8
            prof_raw["area_presets"]["suporte"]["preferred_bullet_ids"] = extra_ids
            settings.profile_path.write_text(
                json.dumps(prof_raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            autofit_res = await client.post(
                "/api/resumes/export-pdf",
                json={"area_id": "suporte", "auto_fit": True},
            )
            assert autofit_res.status_code == 200, autofit_res.text
            af_body = autofit_res.json()
            assert af_body["ats_validation"]["passed"] is True
            assert af_body["ats_validation"]["page_count"] == 1
            assert len(af_body["payload"]["auto_fit_adjustments"]) >= 1
            # Same Chromium instance was reused across both /export-pdf calls!
            assert app.state.pdf_browser_manager.launch_count == 1
