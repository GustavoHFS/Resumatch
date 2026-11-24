"""Phase 4 test suite (v2.0): 100% AI Resume Generation (`POST /api/resumes/generate`),
PIIStripper zero-leakage & local rehydration, `.pdf` and `.docx` Base Resume Import
(`POST /api/profile/import-file`), atomic `PUT /api/profile` backups, and Kanban lifecycle.

Strictly uses fictional fixtures from `examples/`, `httpx.ASGITransport`, and `httpx.MockTransport`.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import app.adapters.llm.providers as llm_providers_mod
from app.adapters.llm.pii_stripper import strip_pii_for_resume_generation
from app.adapters.llm.providers import (
    GeminiAdapter,
    LlmAdapterError,
)
from app.core.config import AppSettings
from app.main import create_app
from app.models import (
    BlockedCompaniesConfig,
    DraftExperienceItem,
    DraftSkillGroup,
    MasterProfile,
    ResumeDraft,
    build_draft_header_from_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "backend" / "tests" / "fixtures" / "demo_examples"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _load_demo_profile_and_blocked() -> tuple[MasterProfile, BlockedCompaniesConfig]:
    profile = MasterProfile.model_validate(
        json.loads((EXAMPLES_DIR / "master_profile.example.json").read_text(encoding="utf-8"))
    )
    blocked = BlockedCompaniesConfig.model_validate(
        json.loads((EXAMPLES_DIR / "blocked_companies.example.json").read_text(encoding="utf-8"))
    )
    return profile, blocked


def _build_minimal_docx_bytes(paragraphs: list[str], table_row: list[str] | None = None) -> bytes:
    body_xml = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    if table_row:
        cells = "".join(
            f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in table_row
        )
        body_xml += f"<w:tbl><w:tr>{cells}</w:tr></w:tbl>"

    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_xml}</w:body></w:document>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        'Target="https://github.com/marina-docx-demo" TargetMode="External"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", doc_xml)
        zf.writestr("word/_rels/document.xml.rels", rels_xml)
    return buf.getvalue()


@asynccontextmanager
async def _asgi_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


class TestPIIStripperAndLlmProvidersV2:
    def test_strips_header_contacts_employers_and_institutions_before_ai_call(self) -> None:
        profile, blocked = _load_demo_profile_and_blocked()
        ctx = strip_pii_for_resume_generation(
            profile=profile,
            blocked=blocked,
            job_title="Estágio Backend Java",
            company="Empresa Alvo Ltda",
            job_description="Requisitos: Java, Spring Boot e SQL. Contato rh@alvo.example.com",
            max_pages=1,
        )
        serialized = json.dumps(ctx.sanitized.model_dump(mode="json"), ensure_ascii=False)

        forbidden_leaks = [
            "Marina Alves Costa",
            "marina.costa@example.com",
            "90000-0000",
            "Savassi",
            "github.com",
            "linkedin.com",
            "Exemplo Outsourcing",
            "Horizonte Tech Solutions",
            "UEEX",
            "rh@alvo.example.com",
        ]
        for secret in forbidden_leaks:
            assert secret.casefold() not in serialized.casefold()

        assert "[[INST_" in ctx.sanitized.base_summary
        assert ctx.sanitized.experiences[0].company_token.startswith("[[EMP_")

    def test_pii_stripper_word_boundary_allows_short_neighborhoods_inside_common_words(self) -> None:
        profile, blocked = _load_demo_profile_and_blocked()
        profile.header.neighborhood = "Pina"
        ctx = strip_pii_for_resume_generation(
            profile=profile,
            blocked=blocked,
            job_title="Analista de Suporte Júnior",
            company="Meta",
            job_description=(
                "Buscamos profissional com disciplina, foco em metas operacionais e centros de custo "
                "para atuação presencial no bairro Pina."
            ),
            max_pages=1,
        )
        serialized = json.dumps(ctx.sanitized.model_dump(mode="json"), ensure_ascii=False)
        assert "disciplina" in serialized
        assert "metas" in serialized
        assert "centros" in serialized
        assert "no bairro [[CANDIDATE_" in ctx.sanitized.target_job_description
        assert "Pina" not in serialized

    @pytest.mark.anyio
    async def test_exclusive_gemini_adapter_validates_connection_falls_back_and_hides_keys(
        self,
    ) -> None:
        # Confirm Groq and Ollama were completely removed from the codebase
        assert not hasattr(llm_providers_mod, "GroqAdapter")
        assert not hasattr(llm_providers_mod, "OllamaAdapter")

        profile, blocked = _load_demo_profile_and_blocked()
        ctx = strip_pii_for_resume_generation(
            profile=profile,
            blocked=blocked,
            job_title="Estágio Desenvolvedor Java",
            job_description="Java, Spring Boot, APIs REST e SQL",
        )
        emp_tok = ctx.sanitized.experiences[0].company_token
        inst_tok = ctx.sanitized.education[0].institution_token

        sample_proposal = json.dumps(
            {
                "target_role": "Desenvolvedora Backend Java",
                "section_order": ["summary", "skills", "experience", "education", "languages"],
                "summary": f"Graduanda na {inst_tok} com foco em Java, Spring Boot e SQL.",
                "experiences": [
                    {
                        "id": "exp_horizonte_support",
                        "role": "Técnica de Suporte de TI",
                        "company": emp_tok,
                        "period": "Mar/2024 – Set/2024",
                        "description": "",
                        "highlights": [
                            "Diagnóstico de incidentes corporativos e administração de contas no Active Directory."
                        ],
                    }
                ],
                "education": [
                    {
                        "id": "edu_1",
                        "degree": "Sistemas para Internet",
                        "institution": inst_tok,
                        "period": "2026 – 2028",
                        "details": "Em andamento",
                    }
                ],
                "courses": [],
                "projects": [],
                "skill_groups": [
                    {"id": "sk_1", "label": "Backend", "items": ["Java", "Spring Boot", "SQL"]}
                ],
                "languages": [
                    {"id": "l1", "language": "Inglês", "proficiency": "Avançado"}
                ],
            },
            ensure_ascii=False,
        )

        fake_gem_key = "test-gem-token-not-real"

        async def no_backoff(_seconds: float) -> None:
            pass

        def gemini_handler(req: httpx.Request) -> httpx.Response:
            assert fake_gem_key not in str(req.url)
            assert req.headers.get("x-goog-api-key") == fake_gem_key
            if req.method == "GET":
                return httpx.Response(200, json={"name": "models/gemini-2.5-flash-lite"})
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": sample_proposal}]}}],
                    "usageMetadata": {
                        "promptTokenCount": 140,
                        "candidatesTokenCount": 110,
                        "totalTokenCount": 250,
                    },
                },
            )

        gem = GeminiAdapter(api_key=fake_gem_key, transport=httpx.MockTransport(gemini_handler))
        assert await gem.validate_connection() == "gemini-2.5-flash-lite"
        gem_res = await gem.generate_tailored_resume(ctx.sanitized)
        assert gem_res.provider == "gemini"
        assert gem_res.token_usage.total_tokens == 250

        gem_err = GeminiAdapter(
            api_key=fake_gem_key,
            sleep=no_backoff,
            transport=httpx.MockTransport(lambda r: httpx.Response(429, text="quota")),
        )
        with pytest.raises(LlmAdapterError) as exc_info:
            await gem_err.generate_tailored_resume(ctx.sanitized)
        assert fake_gem_key not in str(exc_info.value)

        # Verify retry + fallback to Flash Lite when Flash returns 429 or 503.
        called_models: list[str] = []

        def fallback_429_handler(req: httpx.Request) -> httpx.Response:
            called_models.append(str(req.url))
            if "gemini-2.5-flash:" in str(req.url):
                return httpx.Response(503, text="overloaded")
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": sample_proposal}]}}],
                    "usageMetadata": {
                        "promptTokenCount": 100,
                        "candidatesTokenCount": 80,
                        "totalTokenCount": 180,
                    },
                },
            )

        gem_fallback = GeminiAdapter(
            api_key=fake_gem_key,
            model="gemini-2.5-flash", sleep=no_backoff,
            transport=httpx.MockTransport(fallback_429_handler),
        )
        fb_res = await gem_fallback.generate_tailored_resume(ctx.sanitized)
        assert fb_res.model == "gemini-2.5-flash-lite"
        assert any("gemini-2.5-flash" in u for u in called_models)
        assert any("gemini-2.5-flash-lite" in u for u in called_models)


class TestResumeGenerateProfileImportAndKanban:
    @pytest.mark.anyio
    async def test_generate_endpoint_rehydrates_real_header_and_institutions_into_editable_draft(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "v2_gen_ws"
        settings = AppSettings(
            data_dir=workspace,
            examples_dir=EXAMPLES_DIR,
            gemini_api_key="dummy-test-credential",
        )

        def llm_mock(req: httpx.Request) -> httpx.Response:
            sent_body = json.loads(req.content.decode("utf-8"))
            sent_text = sent_body["contents"][0]["parts"][0]["text"]
            assert "Marina Alves Costa" not in sent_text
            assert "marina.costa@example.com" not in sent_text

            sent_payload = json.loads(sent_text)
            emp_tok = sent_payload["experiences"][0]["company_token"]
            inst_tok = sent_payload["education"][0]["institution_token"]

            reply = {
                "target_role": "Desenvolvedora Backend Java Júnior",
                "section_order": ["summary", "skills", "experience", "education", "projects", "languages"],
                "summary": f"Desenvolvedora Java com formação em andamento na {inst_tok} e vivência prática corporativa.",
                "experiences": [
                    {
                        "id": "exp_horizonte_support",
                        "role": "Técnica de Suporte de TI",
                        "company": emp_tok,
                        "period": "Mar/2024 – Set/2024",
                        "description": "",
                        "highlights": [
                            "Diagnóstico estruturado de incidentes técnicos e administração de acessos no Active Directory.",
                            "Automação de planilhas operacionais e documentação técnica de soluções.",
                        ],
                    }
                ],
                "education": [
                    {
                        "id": "edu_sistemas_internet",
                        "degree": "Tecnólogo em Sistemas para Internet",
                        "institution": inst_tok,
                        "period": "Fev/2026 – Dez/2028",
                        "details": "Em andamento",
                    }
                ],
                "courses": [],
                "projects": [],
                "skill_groups": [
                    {"id": "sk_backend", "label": "Backend & Dados", "items": ["Java", "Spring Boot", "SQL"]}
                ],
                "languages": [
                    {"id": "lang_pt", "language": "Português", "proficiency": "Nativo"},
                    {"id": "lang_en", "language": "Inglês", "proficiency": "Avançado"},
                ],
            }
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": json.dumps(reply, ensure_ascii=False)}]}}],
                    "usageMetadata": {"promptTokenCount": 150, "candidatesTokenCount": 120, "totalTokenCount": 270},
                },
            )

        app = create_app(settings=settings, llm_transport=httpx.MockTransport(llm_mock))
        async with _asgi_client(app) as client:
            res = await client.post(
                "/api/resumes/generate",
                json={
                    "job_title": "Desenvolvedora Backend Java Júnior",
                    "company": "Tech Gerais",
                    "job_description": "Atuação com Java, Spring Boot, APIs REST, SQL e diagnóstico de incidentes.",
                    "max_pages": 1,
                },
            )
            assert res.status_code == 200, res.text
            body = res.json()
            draft = body["draft"]
            assert body["provider"] == "gemini"
            assert draft["header"]["full_name"] == "Marina Alves Costa"
            assert draft["header"]["email"] == "marina.costa@example.com"
            assert draft["experiences"][0]["company"] == "Horizonte Tech Solutions"
            assert "Universidade Estadual de Exemplo" in draft["education"][0]["institution"]
            assert "[[EMP_" not in json.dumps(draft, ensure_ascii=False)
            assert "[[INST_" not in json.dumps(draft, ensure_ascii=False)

    @pytest.mark.anyio
    async def test_profile_import_file_supports_docx_and_pdf_with_ai_and_fallback(
        self, tmp_path: Path
    ) -> None:
        # 1. Test Smart Block-Grouping Parser (when Google AI is not connected yet)
        #    explicitly covering the multi-line UNICAP (via Embarque Digital) + UNINTER + ETEMAC education layout!
        ws_fallback = tmp_path / "import_fallback_ws"
        app_fallback = create_app(
            AppSettings(data_dir=ws_fallback, examples_dir=EXAMPLES_DIR, gemini_api_key="")
        )
        docx_bytes = _build_minimal_docx_bytes(
            paragraphs=[
                "Lucas Ferreira Lima",
                "Recife - PE | lucas.lima@example.com | (81) 90000-0000",
                "RESUMO PROFISSIONAL",
                "Profissional de TI com atuação em suporte técnico corporativo, redes e desenvolvimento Python.",
                "EXPERIÊNCIA PROFISSIONAL",
                "Assistente de Suporte de TI",
                "Alfa Sistemas Corporativos",
                "Temporário (CLT) · Mar/2024 – Set/2024 · Recife, PE",
                "Atendimento presencial e remoto em Help Desk, triagem de chamados e configuração de estações.",
                "Administração de contas de usuários no Active Directory e rotinas de backup.",
                "FORMAÇÃO",
                "Graduação em Sistemas para Internet - Em andamento",
                "UNICAP (via Embarque Digital)",
                "Início: Jul/2026 · Previsão de conclusão: Dez/2028",
                "Graduação em Análise e Desenvolvimento de Sistemas",
                "UNINTER",
                "Concluído em Dez/2025",
                "Curso Técnico em Redes de Computadores",
                "ETEMAC",
                "Concluído em Dez/2022",
                "CURSOS E CERTIFICAÇÕES",
                "Arquitetura de Agentes com Python — FIAP · 40 horas · Out/2026: fluxos estruturados e validação.",
                "COMPETÊNCIAS TÉCNICAS",
                "Python, FastAPI, SQL, Windows, Active Directory, Git",
                "IDIOMAS",
                "Português: Nativo • Inglês: Intermediário",
            ],
        )

        async with _asgi_client(app_fallback) as client:
            res_docx = await client.post(
                "/api/profile/import-file",
                files={
                    "file": (
                        "curriculo.docx",
                        docx_bytes,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert res_docx.status_code == 200, res_docx.text
            fb_body = res_docx.json()
            assert fb_body["extraction_mode"] == "heuristic_fallback"
            draft = fb_body["profile_draft"]
            assert draft["header"]["full_name"] == "Lucas Ferreira Lima"
            assert draft["header"]["email"] == "lucas.lima@example.com"
            assert any("github.com/marina-docx-demo" in lnk["url"] for lnk in draft["header"]["links"])
            assert "Python" in draft["skills"]

            # Experience grouped into 1 cohesive block with consolidated description
            assert len(draft["experiences"]) == 1
            assert draft["experiences"][0]["role"] == "Assistente de Suporte de TI"
            assert draft["experiences"][0]["company"] == "Alfa Sistemas Corporativos"
            assert "Active Directory" in draft["experiences"][0]["description"]

            # Education grouped into 3 exact blocks (UNICAP + UNINTER + ETEMAC), never split line-by-line!
            edu = draft["education"]
            assert len(edu) == 3
            assert edu[0]["degree"] == "Graduação em Sistemas para Internet - Em andamento"
            assert edu[0]["institution"] == "UNICAP (via Embarque Digital)"
            assert "Jul/2026" in edu[0]["period"] and "Dez/2028" in edu[0]["period"]
            assert edu[1]["degree"] == "Graduação em Análise e Desenvolvimento de Sistemas"
            assert edu[1]["institution"] == "UNINTER"
            assert edu[1]["period"] == "Concluído em Dez/2025"
            assert edu[2]["degree"] == "Curso Técnico em Redes de Computadores"
            assert edu[2]["institution"] == "ETEMAC"
            assert edu[2]["period"] == "Concluído em Dez/2022"

            # Course separated cleanly into title, institution, workload, completion_date, description
            assert len(draft["courses"]) == 1
            assert draft["courses"][0]["institution"] == "FIAP"
            assert draft["courses"][0]["workload"] == "40 horas"
            assert draft["courses"][0]["completion_date"] == "Out/2026"
            assert "fluxos estruturados" in draft["courses"][0]["description"].lower()

        # 2. Test AI Mode on a real generated .pdf (via Headless Chromium -> pypdf -> AI Structuring)
        ws_ai = tmp_path / "import_ai_ws"

        def import_ai_mock(req: httpx.Request) -> httpx.Response:
            sent = json.loads(req.content.decode("utf-8"))["contents"][0]["parts"][0]["text"]
            # Confirm email & phone were masked before leaving the machine!
            assert "marina.costa@example.com" not in sent
            assert "90000-0000" not in sent
            parsed = {
                "full_name": "Marina Alves Costa",
                "city": "Belo Horizonte",
                "state": "MG",
                "summary": "Desenvolvedora Backend Java e Técnica em Redes.",
                "experiences": [
                    {
                        "role": "Técnica de Suporte de TI",
                        "company": "Horizonte Tech Solutions",
                        "period": "2024",
                        "location": "Belo Horizonte, MG",
                        "description": "Atendimento corporativo Help Desk e administração de Active Directory.",
                    }
                ],
                "education": [
                    {
                        "degree": "Análise e Desenvolvimento de Sistemas",
                        "institution": "Faculdade Horizonte",
                        "period": "2025",
                        "details": "",
                    }
                ],
                "courses": [],
                "projects": [],
                "skills": ["Java", "Spring Boot", "SQL", "Active Directory"],
                "languages": [{"language": "Português", "proficiency": "Nativo"}],
            }
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": json.dumps(parsed, ensure_ascii=False)}]}}],
                    "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 90, "totalTokenCount": 210},
                },
            )

        app_ai = create_app(
            AppSettings(
                data_dir=ws_ai,
                examples_dir=EXAMPLES_DIR,
                gemini_api_key="dummy-key",
            ),
            llm_transport=httpx.MockTransport(import_ai_mock),
        )
        async with _asgi_client(app_ai) as client:
            profile, _ = _load_demo_profile_and_blocked()
            sample_draft = ResumeDraft(
                target_role="Dev Java",
                header=build_draft_header_from_profile(profile.header),
                summary=profile.summary,
                experiences=[
                    DraftExperienceItem(
                        id="e1",
                        role="Técnica de Suporte",
                        company="Horizonte Tech",
                        period="2024",
                        highlights=["Suporte técnico e Active Directory."],
                    )
                ],
                skill_groups=[DraftSkillGroup(id="s1", label="Skills", items=["Java", "SQL"])],
            )
            exported = (
                await client.post(
                    "/api/resumes/export-pdf",
                    json={"draft": sample_draft.model_dump(mode="json")},
                )
            ).json()
            pdf_bytes = (ws_ai / exported["export_rel_path"]).read_bytes()

            res_pdf = await client.post(
                "/api/profile/import-file",
                files={"file": ("curriculo.pdf", pdf_bytes, "application/pdf")},
            )
            assert res_pdf.status_code == 200, res_pdf.text
            ai_body = res_pdf.json()
            assert ai_body["extraction_mode"] == "ai"
            assert ai_body["profile_draft"]["header"]["email"] == "marina.costa@example.com"
            assert "Spring Boot" in ai_body["profile_draft"]["skills"]

    @pytest.mark.anyio
    async def test_atomic_put_profile_and_kanban_lifecycle_with_human_labels(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "v2_kanban_ws"
        settings = AppSettings(
            data_dir=workspace,
            examples_dir=EXAMPLES_DIR,
            max_config_backups=3,
        )
        app = create_app(settings=settings)

        async with _asgi_client(app) as client:
            prof = (await client.get("/api/profile")).json()
            for i in range(1, 6):
                prof["header"]["age"] = 20 + i
                put_res = await client.put("/api/profile", json=prof)
                assert put_res.status_code == 200
            assert len(list(settings.backups_dir.glob("master_profile.*.bak.json"))) == 3

            imp = (
                await client.post(
                    "/api/jobs/import",
                    json={
                        "url": "https://jobs.example.com/vagas/suporte-bh-50",
                        "title": "Técnico de Suporte Júnior",
                        "company": "Tech Horizonte",
                        "location": "Belo Horizonte, MG",
                        "workplace_mode": "presencial",
                        "description": "CLT 08h às 17h. Requisitos: Windows, Active Directory e Help Desk.",
                    },
                )
            ).json()
            opp_id = imp["opportunity_id"]

            enq_res = await client.post(
                "/api/applications",
                json={"opportunity_id": opp_id, "stage": "candidatura_a_preparar"},
            )
            assert enq_res.status_code == 201
            assert enq_res.json()["stage_label"] == "A preparar"
            assert "_" not in enq_res.json()["labels"]["compatibility_label"]

            profile_model = MasterProfile.model_validate(prof)
            draft = ResumeDraft(
                target_role="Técnico de Suporte Júnior",
                company="Tech Horizonte",
                header=build_draft_header_from_profile(profile_model.header),
                summary="Profissional com vivência em Windows, Active Directory e Help Desk.",
                experiences=[
                    DraftExperienceItem(
                        id="e1",
                        role="Técnica de Suporte de TI",
                        company="Horizonte Tech Solutions",
                        period="2024",
                        highlights=["Atendimento Help Desk e administração Active Directory."],
                    )
                ],
                skill_groups=[
                    DraftSkillGroup(id="g1", label="Suporte", items=["Windows", "Active Directory"])
                ],
            )
            pdf_exp = (
                await client.post(
                    "/api/resumes/export-pdf",
                    json={"draft": draft.model_dump(mode="json"), "opportunity_id": opp_id},
                )
            ).json()
            version_id = pdf_exp["version_id"]
            pdf_sha = pdf_exp["pdf_sha256"]

            unconfirmed = await client.patch(
                f"/api/applications/{opp_id}/stage",
                json={"stage": "enviada_manualmente", "confirm_manual_submission": False},
            )
            assert unconfirmed.status_code == 422

            sent_res = await client.patch(
                f"/api/applications/{opp_id}/stage",
                json={
                    "stage": "enviada_manualmente",
                    "resume_version_id": version_id,
                    "confirm_manual_submission": True,
                    "notes": "Enviado pelo portal da empresa",
                },
            )
            assert sent_res.status_code == 200
            card = sent_res.json()
            assert card["stage"] == "enviada_manualmente"
            assert card["stage_label"] == "Enviada"
            assert card["resume_version"]["pdf_sha256"] == pdf_sha

            del_res = await client.delete(f"/api/applications/{opp_id}")
            assert del_res.status_code == 200
            assert (await client.get("/api/applications")).json()["total"] == 0
