"""A4 rules, human-owned education ordering and lossless elastic page fitting."""

from datetime import date
from io import BytesIO
from pathlib import Path
import json
import sys
import mimetypes

import pytest
from pypdf import PdfReader
from sqlmodel import Session, SQLModel, create_engine
from playwright.sync_api import expect, sync_playwright

from app.adapters.llm.pii_stripper import rehydrate_generated_draft, strip_pii_for_resume_generation
from app.adapters.llm.providers import build_resume_generation_prompts
from app.adapters.pdf.browser_manager import ChromiumPdfBrowserManager
from app.adapters.pdf.html_renderer import render_resume_a4_html
from app.core.config import AppSettings
from app.domain.resume_policy import academic_order, clean_academic_text, concise_summary
from app.models import MasterProfile, ResumeDraft, DraftExperienceItem, BlockedCompaniesConfig
from app.services.resume_service import ResumeExportPdfRequest, export_validated_pdf, _shrink_draft_for_budget
from scripts.check_privacy import run_scan
from scripts import build_launcher_exe


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def profile() -> MasterProfile:
    return MasterProfile.model_validate({
        "header": {"full_name": "Candidata de Teste", "city": "Cidade Exemplo", "state": "EX",
                   "phone": "(00) 90000-0000", "email": "demo@example.com"},
        "education": [
            {"id": "tech", "degree": "Curso Técnico em Redes", "institution": "Escola Exemplo",
             "period": "Concluído em 2024", "details": "Foco em redes locais"},
            {"id": "grad", "degree": "Bacharelado em Computação", "institution": "Universidade Exemplo",
             "period": "Concluído em 2025"},
            {"id": "current", "degree": "Graduação em Sistemas (Foco em suporte)",
             "institution": "Faculdade Exemplo ((via Programa Exemplo))",
             "period": "2026 – 2028 (Em andamento)", "details": "Ementa que não deve ser publicada"},
        ],
    })


def draft() -> ResumeDraft:
    base = profile()
    return rehydrate_generated_draft(
        proposal_dict={"target_role": "HEADLINE NÃO RENDERIZAR", "summary": "Estudante com prática em suporte."},
        profile=base, max_pages=1, company=None, placeholder_to_original={},
    )


def test_education_default_order_and_manual_override_survive_ai() -> None:
    base = profile()
    assert [item.id for item in base.education] == ["current", "grad", "tech"]
    assert [item.id for item in academic_order(base.education, date(2026, 11, 20))] == ["current", "grad", "tech"]
    manual = MasterProfile.model_validate({**base.model_dump(), "education": list(reversed(base.education)),
                                         "education_order_manual": True})
    result = rehydrate_generated_draft(
        proposal_dict={"education": [{"degree": "Curso Técnico inventado", "details": "Observação"}]},
        profile=manual, max_pages=1, company=None, placeholder_to_original={},
    )
    assert [item.id for item in result.education] == ["tech", "grad", "current"]
    assert all(item.details == "" for item in result.education)
    assert all("(" not in item.degree + item.institution + item.period for item in result.education)
    assert "Em andamento" in result.education[-1].period
    assert "Ementa" in base.education[0].details  # Source facts remain intact.


@pytest.mark.parametrize("text", [
    "Formada em Sistemas de Informação. " + "Atuação prática em suporte e documentação. " * 30,
    "Experiência no atendimento a usuários; " + "documentação de processos e organização de chamados " * 30,
    "Estudante com prática em suporte " * 40,
])
def test_summary_is_one_paragraph_with_complete_word_boundaries(text: str) -> None:
    result = concise_summary(text.replace(". ", ".\n"))
    assert len(result) <= 420
    assert "\n" not in result
    assert result.endswith((".", "!", "?"))
    assert not result.endswith("…")
    assert ResumeDraft(header=draft().header, summary=text).summary == concise_summary(text)


def test_prompt_and_academic_sanitization() -> None:
    base = profile()
    context = strip_pii_for_resume_generation(profile=base, blocked=BlockedCompaniesConfig(),
        job_title="Suporte", company=None, job_description="Suporte técnico.", max_pages=1)
    prompt, _ = build_resume_generation_prompts(context.sanitized)
    assert "420 caracteres" in prompt and "3 a 4 linhas" in prompt
    assert "ordem recebida" in prompt and "details deve ser vazio" in prompt
    assert all(not item.details for item in context.sanitized.education)
    assert clean_academic_text("Faculdade ((via Programa))") == "Faculdade"
    assert clean_academic_text("(2026–2028)", keep_status=True) == "2026–2028"


def test_html_has_only_three_education_fields_and_no_headline() -> None:
    for preview in [True, False]:
        html = render_resume_a4_html(draft(), standalone_preview=preview)
        header = html.split('<header class="resume-header">')[1].split("</header>")[0]
        assert "HEADLINE" not in header
        assert header.index("Candidata de Teste") < header.index("Cidade Exemplo") < header.index("demo@example.com")
        assert "Ementa" not in html and "Foco em suporte" not in html and "via Programa" not in html
        assert "text-align: justify" not in html


def test_layout_steps_only_change_geometry_not_facts() -> None:
    original = draft().model_copy(update={"experiences": [DraftExperienceItem(
        role="Suporte", highlights=[f"Entrega de suporte número {i}." for i in range(12)])]})
    for step, margin in enumerate([14, 12, 10.5, 9.5]):
        compact = _shrink_draft_for_budget(original, step)
        assert compact.education == original.education
        assert compact.experiences == original.experiences
        assert compact.courses == original.courses
        assert f"margin: {float(margin)}mm" in render_resume_a4_html(compact)


def test_preview_and_pdf_css_have_same_metrics_and_fixed_font_size() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            for step, margin in enumerate([14, 12, 10.5, 9.5]):
                html = render_resume_a4_html(draft().model_copy(update={"layout_step": step}))
                # Inline fonts allow an offline DOM check for both media modes.
                html = html.replace('<body class="">', '<body class="standalone-preview">')
                page.set_content(html)
                page.evaluate("document.fonts.ready")
                metrics = []
                for media in ["screen", "print"]:
                    page.emulate_media(media=media)
                    metrics.append(page.locator(".summary-paragraph").evaluate(
                        "el => {const s=getComputedStyle(el);return [s.fontSize,s.lineHeight,s.textAlign]}"))
                assert metrics[0] == metrics[1]
                assert metrics[0][2] == "left"
                assert float(metrics[0][0].replace("px", "")) >= 13
        finally:
            browser.close()


@pytest.mark.anyio
async def test_elastic_export_fits_one_page_and_preserves_every_bullet(tmp_path: Path) -> None:
    document = draft().model_copy(update={
        "education": [], "summary": "Experiência em suporte técnico.",
        "experiences": [DraftExperienceItem(role="Suporte técnico", highlights=[
            f"Entrega {i:02d}: atendimento e documentação de chamados técnicos." for i in range(40)
        ])],
    })
    manager = ChromiumPdfBrowserManager()
    try:
        normal_pdf = await manager.render_html_to_pdf(render_resume_a4_html(document))
        assert len(PdfReader(BytesIO(normal_pdf)).pages) > 1
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            exported = await export_validated_pdf(session, AppSettings(data_dir=tmp_path), manager,
                                                 ResumeExportPdfRequest(draft=document))
        assert exported["ats_validation"]["page_count"] == 1
        assert exported["draft"]["layout_step"] > 0
        assert len(exported["draft"]["experiences"][0]["highlights"]) == 40
        pdf = PdfReader(BytesIO((tmp_path / exported["export_rel_path"]).read_bytes()))
        text = " ".join(page.extract_text() for page in pdf.pages)
        assert "HEADLINE" not in text
        assert all(f"Entrega {i:02d}:" in text for i in range(40))
    finally:
        await manager.aclose()


@pytest.mark.anyio
async def test_pdf_has_no_headline_or_academic_notes_and_keeps_status() -> None:
    manager = ChromiumPdfBrowserManager()
    try:
        pdf = await manager.render_html_to_pdf(render_resume_a4_html(draft()))
        reader = PdfReader(BytesIO(pdf))
        assert len(reader.pages) == 1
        text = " ".join(page.extract_text() for page in reader.pages)
        assert "HEADLINE" not in text
        assert "Foco em suporte" not in text and "via Programa" not in text and "Ementa" not in text
        assert "Em andamento" in text
        assert text.index("Graduação em Sistemas") < text.index("Bacharelado") < text.index("Curso Técnico")
    finally:
        await manager.aclose()


def test_clean_checkout_privacy_has_no_local_fingerprint_dependency(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Clean checkout", encoding="utf-8")
    assert run_scan(tmp_path, tmp_path / "user_data") == ([], 1, 0)
    assert not (tmp_path / "user_data").exists()


def test_linux_launcher_fails_early_without_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="requires Windows"):
        build_launcher_exe.build_exe()


def test_ci_validates_linux_but_only_packages_windows() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/ci-and-release.yml").read_text(encoding="utf-8")
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "install --with-deps chromium-headless-shell" in workflow
    assert workflow.count("&& runner.os == 'Windows'") == 3


@pytest.mark.skipif(sys.platform != "win32", reason="Native C# launcher compilation requires Windows csc.exe")
def test_native_launcher_compiles_in_temporary_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_launcher_exe, "OUTPUT_EXE", tmp_path / "Resumatch.exe")
    assert build_launcher_exe.build_exe().read_bytes().startswith(b"MZ")


def test_education_buttons_in_base_and_studio_use_only_mocked_api() -> None:
    root = Path(__file__).resolve().parents[2]
    dist = root / "frontend/dist"
    if not (dist / "index.html").is_file():
        pytest.skip("Compile frontend antes de testar os controles React")
    base = profile().model_dump(mode="json")
    saves: list[dict] = []
    previews: list[dict] = []

    def serve(route) -> None:
        from urllib.parse import urlsplit
        url = urlsplit(route.request.url)
        if url.hostname != "resumatch.test":
            route.abort()
            return
        path = url.path
        response = {}
        if path == "/api/health":
            response = {"ai_connected": True}
        elif path == "/api/profile":
            if route.request.method == "PUT":
                saves.append(route.request.post_data_json)
                response = {"status": "updated", "profile": saves[-1]}
            else:
                response = base
        elif path in {"/api/applications", "/api/jobs"}:
            response = {"total": 0, "items": []}
        elif path == "/api/resumes/generate":
            response = {"draft": draft().model_dump(mode="json"), "job_context": {"opportunity_id": None}}
        elif path == "/api/resumes/render-html":
            payload = route.request.post_data_json["draft"]
            previews.append(payload)
            document = ResumeDraft.model_validate(payload)
            response = {"draft": document.model_dump(mode="json"), "html": render_resume_a4_html(document)}
        elif not path.startswith("/api/"):
            asset = dist / path.lstrip("/") if path != "/" else dist / "index.html"
            route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
            return
        route.fulfill(json=response)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/*", serve)
            page.goto("http://resumatch.test/")
            page.get_by_role("button", name="Currículo Base", exact=True).click()
            expect(page.get_by_role("button", name="Subir formação").first).to_be_disabled()
            page.get_by_role("button", name="Descer formação").first.click()
            expect(page.get_by_text("Salvo automaticamente", exact=True)).to_be_visible()
            assert saves and saves[-1]["education_order_manual"] is True
            assert [item["id"] for item in saves[-1]["education"]] == ["grad", "current", "tech"]
            page.get_by_role("button", name="Estúdio", exact=True).click()
            page.get_by_placeholder("Cole aqui o texto da vaga...").fill("Suporte técnico de exemplo.")
            page.get_by_role("button", name="Gerar com IA", exact=True).click()
            page.get_by_role("button", name="Formação Acadêmica", exact=True).click()
            page.get_by_role("button", name="Descer formação").first.click()
            expect(page.frame_locator('iframe[title="Pré-visualização A4"]').locator(
                '[data-section="education"] .entry-title-line').first).to_contain_text("Bacharelado")
            assert [item["id"] for item in previews[-1]["education"]] == ["grad", "current", "tech"]
            header = page.frame_locator('iframe[title="Pré-visualização A4"]').locator(".resume-header")
            expect(header).not_to_contain_text("HEADLINE")
        finally:
            browser.close()
