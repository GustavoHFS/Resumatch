"""Phase 4 test suite: Surgical AI adapters (Gemini, Groq, Ollama), PIIStripper zero-leakage,
VeracityGuard granular per-block fallbacks, atomic PUT /api/profile with rotating backups,
and audited Kanban /api/applications lifecycle.

Strictly uses fictional fixtures from `examples/`, `httpx.ASGITransport`, and `httpx.MockTransport`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.adapters.llm.pii_stripper import strip_pii_for_surgical_llm
from app.adapters.llm.providers import (
    GeminiAdapter,
    GroqAdapter,
    LlmAdapterError,
    OllamaAdapter,
)
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


class TestPIIStripperZeroLeakage:
    def test_strips_header_blocked_employers_and_masks_institutions(self) -> None:
        profile, criteria, blocked = _load_demo_domain()
        triage_engine = DeterministicTriageEngine(criteria, blocked, profile)
        composer = DeterministicResumeComposer(profile)

        job_desc = (
            "Estágio em Desenvolvimento Java. Requisitos: Java, Spring Boot, SQL, GLPI e Docker."
        )
        triage = triage_engine.evaluate(
            RawJobInput(
                title="Estágio em Desenvolvimento Java",
                company="Empresa Neutra",
                url="https://jobs.example.com/java",
                location="Belo Horizonte, MG",
                workplace_mode="presencial",
                description=job_desc,
            )
        )
        payload = composer.compose("dev", triage=triage, job_description=job_desc)

        ctx = strip_pii_for_surgical_llm(
            payload=payload,
            profile=profile,
            blocked=blocked,
            triage=triage,
            extra_job_keywords=["Spring Boot", "GLPI", "Docker"],
        )
        outbound_json = json.dumps(ctx.sanitized.model_dump(mode="json"), ensure_ascii=False)

        # Zero PII, zero institution names, zero employer names, zero blocked company names
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
            "Universidade Estadual de Exemplo",
        ]
        for secret in forbidden_leaks:
            assert secret.casefold() not in outbound_json.casefold()

        # UEEX inside summary was masked with [[INST_*]] placeholder
        assert "[[INST_" in ctx.sanitized.summary_text
        # Unverified / preparable skills ('GLPI', 'Docker') NEVER enter confirmed_job_keywords
        assert "GLPI" not in ctx.sanitized.confirmed_job_keywords
        assert "Docker" not in ctx.sanitized.confirmed_job_keywords
        assert "Java" in ctx.sanitized.confirmed_job_keywords


class TestLightweightHttpxLlmAdapters:
    @pytest.mark.anyio
    async def test_gemini_groq_and_ollama_adapters_parse_structured_outputs_and_hide_keys(
        self,
    ) -> None:
        profile, _, blocked = _load_demo_domain()
        composer = DeterministicResumeComposer(profile)
        det_payload = composer.compose("dev")
        ctx = strip_pii_for_surgical_llm(det_payload, profile, blocked)

        proposal_json = json.dumps(
            {
                "summary_text": ctx.sanitized.summary_text,
                "bullets": [
                    {"bullet_id": b.bullet_id, "text": b.text} for b in ctx.sanitized.bullets
                ],
            },
            ensure_ascii=False,
        )

        # 1. GeminiAdapter (key in x-goog-api-key header, NEVER in URL)
        fake_gem_key = "test-gem-token-not-real"

        def gemini_handler(req: httpx.Request) -> httpx.Response:
            assert fake_gem_key not in str(req.url)
            assert req.headers.get("x-goog-api-key") == fake_gem_key
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": proposal_json}]}}],
                    "usageMetadata": {
                        "promptTokenCount": 110,
                        "candidatesTokenCount": 85,
                        "totalTokenCount": 195,
                    },
                },
            )

        gem = GeminiAdapter(
            api_key=fake_gem_key,
            model="gemini-2.5-flash",
            transport=httpx.MockTransport(gemini_handler),
        )
        gem_res = await gem.adapt_once(ctx.sanitized)
        assert gem_res.provider == "gemini"
        assert gem_res.token_usage.total_tokens == 195

        # Error path never echoes key
        gem_err = GeminiAdapter(
            api_key=fake_gem_key,
            transport=httpx.MockTransport(lambda r: httpx.Response(429, text="quota")),
        )
        with pytest.raises(LlmAdapterError) as exc_info:
            await gem_err.adapt_once(ctx.sanitized)
        assert fake_gem_key not in str(exc_info.value)

        # 2. GroqAdapter
        fake_groq_key = "test-groq-token-not-real"

        def groq_handler(req: httpx.Request) -> httpx.Response:
            assert req.headers.get("authorization") == f"Bearer {fake_groq_key}"
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": proposal_json}}],
                    "usage": {"prompt_tokens": 90, "completion_tokens": 70, "total_tokens": 160},
                },
            )

        groq = GroqAdapter(
            api_key=fake_groq_key,
            transport=httpx.MockTransport(groq_handler),
        )
        groq_res = await groq.adapt_once(ctx.sanitized)
        assert groq_res.provider == "groq"
        assert groq_res.token_usage.total_tokens == 160

        # 3. OllamaAdapter
        def ollama_handler(req: httpx.Request) -> httpx.Response:
            assert str(req.url) == "http://127.0.0.1:11434/api/chat"
            return httpx.Response(
                200,
                json={
                    "message": {"content": proposal_json},
                    "prompt_eval_count": 100,
                    "eval_count": 60,
                },
            )

        ollama = OllamaAdapter(transport=httpx.MockTransport(ollama_handler))
        oll_res = await ollama.adapt_once(ctx.sanitized)
        assert oll_res.provider == "ollama"
        assert oll_res.token_usage.total_tokens == 160


class TestVeracityGuardProfilePutAndApplicationsKanban:
    @pytest.mark.anyio
    async def test_surgical_adapt_veracity_guard_reverts_hallucinations_and_keeps_valid_edits(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "phase4_adapt_ws"
        settings = AppSettings(
            data_dir=workspace,
            examples_dir=EXAMPLES_DIR,
            llm_provider="gemini",
            gemini_api_key="dummy-test-credential",
            max_config_backups=3,
        )

        def adversarial_llm_mock(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode("utf-8"))
            sent_text = body["contents"][0]["parts"][0]["text"]
            # Confirm zero PII in actual HTTP request payload
            assert "Marina Alves Costa" not in sent_text
            assert "UEEX" not in sent_text

            sent_payload = json.loads(sent_text)
            # Extract the [[INST_*]] token used in summary_text so valid summary rehydrates cleanly
            inst_tok = next(
                tok for tok in sent_payload["summary_text"].split() if tok.startswith("[[INST_")
            ).rstrip(".,")

            adversarial_proposal = {
                # VALID edit: natural ATS refinement preserving [[INST_*]] and zero invented facts
                "summary_text": (
                    f"Graduanda em Sistemas para Internet na {inst_tok} e tecnóloga em ADS, "
                    "com foco em backend Java, Spring Boot, APIs REST e bancos relacionais SQL."
                ),
                "bullets": [
                    # INVALID #1: invents percentage metric '40%' not in original bullet
                    {
                        "bullet_id": "bul_helpdesk_tickets",
                        "text": "Atendimento Help Desk corporativo reduzindo tempo de fila em 40%.",
                    },
                    # INVALID #2: hallucinates 'GLPI' (preparable_gap, can_appear_on_resume=False)
                    {
                        "bullet_id": "bul_ad_email",
                        "text": "Administração de contas no Active Directory e gestão de tickets no GLPI.",
                    },
                    # INVALID #3: injects self-deprecating 'Excel básico'
                    {
                        "bullet_id": "bul_spreadsheets_reports",
                        "text": "Controle de orçamentos em planilhas com Excel básico e relatórios.",
                    },
                ],
            }
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": json.dumps(adversarial_proposal, ensure_ascii=False)}]
                            }
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 95, "totalTokenCount": 215},
                },
            )

        app = create_app(
            settings=settings,
            llm_transport=httpx.MockTransport(adversarial_llm_mock),
        )

        async with _asgi_client(app) as client:
            res = await client.post(
                "/api/resumes/adapt",
                json={
                    "area_id": "dev",
                    "job_title": "Estágio Backend Java",
                    "job_description": "Estágio remunerado 09h às 15h em Belo Horizonte. Requisitos: Java, Spring Boot, APIs REST e SQL.",
                },
            )
            assert res.status_code == 200, res.text
            data = res.json()

            assert data["provider"] == "gemini"
            assert data["token_usage"]["total_tokens"] == 215

            # Summary passed guard and rehydrated [[INST_*]] back to 'UEEX'!
            assert "UEEX" in data["payload"]["summary"]["text"]
            assert "[[INST_" not in data["payload"]["summary"]["text"]

            # All 3 adversarial bullets were caught and reverted to deterministic originals!
            rules_triggered = {w["block_id"]: w["rule"] for w in data["guard_warnings"]}
            assert rules_triggered["bul_helpdesk_tickets"] == "INVENTED_METRIC_OR_NUMBER"
            assert rules_triggered["bul_ad_email"] == "UNVERIFIED_SKILL_HALLUCINATION"
            assert rules_triggered["bul_spreadsheets_reports"] == "SELF_DEPRECATING_QUALIFIER"

            diff_by_id = {d["block_id"]: d for d in data["diff"]}
            assert diff_by_id["summary"]["changed"] is True
            assert diff_by_id["summary"]["reverted_by_guard"] is False
            assert diff_by_id["bul_helpdesk_tickets"]["reverted_by_guard"] is True
            assert diff_by_id["bul_helpdesk_tickets"]["after"] == diff_by_id["bul_helpdesk_tickets"]["before"]

    @pytest.mark.anyio
    async def test_atomic_put_profile_criteria_blocked_with_rotating_backups(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "phase4_config_ws"
        settings = AppSettings(
            data_dir=workspace,
            examples_dir=EXAMPLES_DIR,
            max_config_backups=3,
        )
        app = create_app(settings=settings)

        async with _asgi_client(app) as client:
            prof = (await client.get("/api/profile")).json()

            # Perform 5 valid updates -> rotating backup must cap at max_config_backups=3
            for i in range(1, 6):
                prof["header"]["age"] = 20 + i
                put_res = await client.put("/api/profile", json=prof)
                assert put_res.status_code == 200
                assert put_res.json()["backup_snapshot"] is not None

            updated_prof = (await client.get("/api/profile")).json()
            assert updated_prof["header"]["age"] == 25

            backups = list(settings.backups_dir.glob("master_profile.*.bak.json"))
            assert len(backups) == 3

            # Invalid update (self-deprecating label) must fail 422 without corrupting file
            bad_prof = json.loads(json.dumps(updated_prof))
            bad_prof["skills"][0]["display_name"] = "Java básico"
            bad_res = await client.put("/api/profile", json=bad_prof)
            assert bad_res.status_code == 422
            assert (await client.get("/api/profile")).json()["skills"][0]["display_name"] == "Java"

            # PUT /api/config/criteria and GET/PUT /api/config/blocked
            criteria = (await client.get("/api/config/criteria")).json()
            criteria["career_focus"]["preferred_recency_days"] = 45
            assert (await client.put("/api/config/criteria", json=criteria)).status_code == 200

            blocked = (await client.get("/api/config/blocked")).json()
            assert len(blocked["companies"]) == 1
            assert (await client.put("/api/config/blocked", json=blocked)).status_code == 200

    @pytest.mark.anyio
    async def test_applications_kanban_stage_transitions_and_audit_trail(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "phase4_kanban_ws"
        settings = AppSettings(data_dir=workspace, examples_dir=EXAMPLES_DIR)
        app = create_app(settings=settings)

        async with _asgi_client(app) as client:
            imp = (
                await client.post(
                    "/api/jobs/import",
                    json={
                        "url": "https://jobs.example.com/vagas/suporte-bh-50",
                        "title": "Técnico de Suporte Júnior",
                        "company": " Tech Horizonte ",
                        "location": "Belo Horizonte, MG",
                        "workplace_mode": "presencial",
                        "description": "CLT 08h às 17h. Requisitos: Windows, Active Directory e Help Desk.",
                    },
                )
            ).json()
            opp_id = imp["opportunity_id"]

            # 1. Enqueue on Kanban
            enq_res = await client.post(
                "/api/applications",
                json={"opportunity_id": opp_id, "stage": "candidatura_a_preparar"},
            )
            assert enq_res.status_code == 201
            assert enq_res.json()["stage"] == "candidatura_a_preparar"

            # 2. Cannot jump to 'curriculo_preparado' without a valid ResumeVersion
            no_ver = await client.patch(
                f"/api/applications/{opp_id}/stage",
                json={"stage": "curriculo_preparado"},
            )
            assert no_ver.status_code == 422

            # 3. Prohibited 'enviada_automaticamente' is always rejected
            auto_res = await client.patch(
                f"/api/applications/{opp_id}/stage",
                json={"stage": "enviada_automaticamente"},
            )
            assert auto_res.status_code == 422

            # 4. Export validated A4 PDF -> links ResumeVersion + pdf_sha256
            pdf_exp = (
                await client.post(
                    "/api/resumes/export-pdf",
                    json={"area_id": "suporte", "opportunity_id": opp_id},
                )
            ).json()
            version_id = pdf_exp["version_id"]
            pdf_sha = pdf_exp["pdf_sha256"]

            # 5. Move to 'aguardando_envio_manual'
            wait_res = await client.patch(
                f"/api/applications/{opp_id}/stage",
                json={"stage": "aguardando_envio_manual", "resume_version_id": version_id},
            )
            assert wait_res.status_code == 200
            assert wait_res.json()["stage"] == "aguardando_envio_manual"

            # 6. Marking 'enviada_manualmente' without confirm_manual_submission=True fails 422
            unconfirmed = await client.patch(
                f"/api/applications/{opp_id}/stage",
                json={"stage": "enviada_manualmente", "confirm_manual_submission": False},
            )
            assert unconfirmed.status_code == 422

            # 7. Marking 'enviada_manualmente' with confirm_manual_submission=True succeeds & locks version hash
            sent_res = await client.patch(
                f"/api/applications/{opp_id}/stage",
                json={
                    "stage": "enviada_manualmente",
                    "confirm_manual_submission": True,
                    "notes": "Submitted PDF via company portal",
                },
            )
            assert sent_res.status_code == 200
            card = sent_res.json()
            assert card["stage"] == "enviada_manualmente"
            assert card["user_confirmed_at"] is not None
            assert card["resume_version"]["id"] == version_id
            assert card["resume_version"]["pdf_sha256"] == pdf_sha

            # 8. Listing & Events endpoint verification
            listed = (await client.get("/api/applications?stage=enviada_manualmente")).json()
            assert listed["total"] == 1
            assert listed["items"][0]["resume_version"]["pdf_sha256"] == pdf_sha

            events = (await client.get(f"/api/applications/{opp_id}/events")).json()
            stages_logged = [ev["stage"] for ev in events]
            assert "candidatura_a_preparar" in stages_logged
            assert "curriculo_preparado" in stages_logged
            assert "aguardando_envio_manual" in stages_logged
            assert stages_logged[-1] == "enviada_manualmente"
            assert pdf_sha[:12] in (events[-1]["notes"] or "")
