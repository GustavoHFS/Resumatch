"""Phase 2 test suite: SafeAsyncHttpClient, connectors, DeterministicTriageEngine,
assisted manual import, canonical deduplication, and APScheduler REST routes.

Follows strict anti-hang testing rules:
- Zero background `uvicorn` processes.
- All FastAPI routes tested via `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`.
- All external HTTP/RSS/Sitemap/JSON-LD/MCP/robots.txt calls simulated via `httpx.MockTransport`.
- Uses exclusively public fictional fixtures from `examples/`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import Session

from app.connectors.http_client import (
    AccessRestrictedError,
    BudgetExhaustedError,
    ConnectorError,
    SafeAsyncHttpClient,
)
from app.core.config import AppSettings
from app.domain.triage_engine import DeterministicTriageEngine, RawJobInput
from app.main import create_app
from app.models import (
    BlockedCompaniesConfig,
    JobOpportunity,
    MasterProfile,
    SearchCriteria,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _load_triage_engine() -> DeterministicTriageEngine:
    profile = MasterProfile.model_validate(
        json.loads((EXAMPLES_DIR / "master_profile.example.json").read_text(encoding="utf-8"))
    )
    criteria = SearchCriteria.model_validate(
        json.loads((EXAMPLES_DIR / "search_criteria.example.json").read_text(encoding="utf-8"))
    )
    blocked = BlockedCompaniesConfig.model_validate(
        json.loads((EXAMPLES_DIR / "blocked_companies.example.json").read_text(encoding="utf-8"))
    )
    return DeterministicTriageEngine(criteria=criteria, blocked=blocked, profile=profile)


@asynccontextmanager
async def _asgi_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


class TestDeterministicTriageEngine:
    def test_entry_level_java_job_with_short_prep_gap_and_tier1_email(self) -> None:
        engine = _load_triage_engine()
        job = RawJobInput(
            source="rss_local:demo",
            source_id="job-101",
            title="Estágio em Desenvolvimento Backend Java",
            company="Minas Digital",
            url="https://jobs.example.com/vagas/estagio-java-101",
            location="Belo Horizonte, MG",
            workplace_mode="presencial",
            validity_status="confirmadamente_aberta",
            pdf_confirmed=True,
            description=(
                "Estágio remunerado com bolsa R$ 1.800. Horário: 08h às 14h (segunda a sexta).\n"
                "Requisitos:\n"
                "- Conhecimentos em Java, POO, Spring Boot e SQL.\n"
                "- Conhecimento em JUnit para testes unitários.\n"
                "Desejáveis:\n"
                "- Certificação AWS (diferencial, não obrigatória).\n"
                "Envie seu currículo em PDF para talentos@minasdigital.example.com"
            ),
        )
        res = engine.evaluate(job)

        assert res.area_id == "dev"
        assert res.career_tier == "entrada"
        assert res.hard_incompatibilities == []
        assert res.classification == "aprovada_com_preparacao"
        assert res.professional_compatibility == "compativel_com_preparacao"
        assert res.local_prep_allowed is True
        assert "Java" in res.matched_skills
        assert "Spring Boot" in res.matched_skills
        assert "JUnit" in res.preparable_gaps
        assert any(p.term == "JUnit" for p in res.short_prep_plans)
        # Ensure preparable gap is NEVER marked allowed on resume
        junit_req = next(r for r in res.requirements if r.skill_name == "JUnit")
        assert junit_req.can_appear_on_resume is False
        # Direct email + complete text -> Tier 1 Method A
        assert res.application_method == "A"
        assert res.service_tier == 1
        assert res.automation_capability == "automacao_possivel"

    def test_rejects_blocked_company_and_recruiting_on_behalf(self) -> None:
        engine = _load_triage_engine()
        direct_blocked = RawJobInput(
            title="Auxiliar de Suporte Técnico",
            company="Exemplo Outsourcing S.A.",
            url="https://jobs.example.com/1",
            location="Belo Horizonte, MG",
            description="Vaga CLT diurna de suporte técnico Windows e redes.",
        )
        res_direct = engine.evaluate(direct_blocked)
        assert res_direct.classification == "descartada"
        assert res_direct.professional_compatibility == "incompativel_por_impedimento_concreto"
        assert any("Empresa bloqueada" in msg for msg in res_direct.hard_incompatibilities)

        on_behalf = RawJobInput(
            title="Auxiliar de Suporte Técnico",
            company="Consultoria RH Neutro",
            url="https://jobs.example.com/2",
            location="Belo Horizonte, MG",
            description="Estamos recrutando para Exemplo Outsourcing profissional CLT de suporte Windows.",
        )
        res_behalf = engine.evaluate(on_behalf)
        assert res_behalf.classification == "descartada"
        assert any("Empresa bloqueada" in msg for msg in res_behalf.hard_incompatibilities)

    @pytest.mark.parametrize(
        ("kwargs", "expected_reason_substr"),
        [
            (
                {
                    "title": "Desenvolvedor Júnior",
                    "company": "Empresa Ok",
                    "url": "https://jobs.example.com/pj",
                    "location": "Belo Horizonte, MG",
                    "description": "Contratação exclusivamente PJ / MEI. Horário comercial.",
                },
                "Vínculo contratual excluído",
            ),
            (
                {
                    "title": "Técnico de Suporte N1",
                    "company": "Empresa Ok",
                    "url": "https://jobs.example.com/night",
                    "location": "Belo Horizonte, MG",
                    "description": "Regime CLT. Escala em turno noturno / madrugada (22h às 06h).",
                },
                "noturno",
            ),
            (
                {
                    "title": "Assistente de TI",
                    "company": "Empresa Ok",
                    "url": "https://jobs.example.com/sp",
                    "location": "São Paulo, SP",
                    "workplace_mode": "presencial",
                    "description": "Atuação 100% presencial em São Paulo, CLT, 09h às 18h.",
                },
                "fora dos municípios permitidos",
            ),
            (
                {
                    "title": "Analista Júnior Java",
                    "company": "Empresa Ok",
                    "url": "https://jobs.example.com/exp",
                    "location": "Belo Horizonte, MG",
                    "description": "CLT 09h às 18h. Requisitos: Experiência mínima obrigatória de 3 anos com Java.",
                },
                "Experiência mínima explícita",
            ),
        ],
    )
    def test_hard_incompatibilities_are_rejected(
        self, kwargs: dict, expected_reason_substr: str
    ) -> None:
        engine = _load_triage_engine()
        res = engine.evaluate(RawJobInput(**kwargs))
        assert res.classification == "descartada"
        assert res.local_prep_allowed is False
        assert any(expected_reason_substr in item for item in res.hard_incompatibilities)

    def test_corporate_history_years_do_not_trigger_experience_rejection(self) -> None:
        engine = _load_triage_engine()
        job = RawJobInput(
            title="Estágio de Suporte Técnico",
            company="Tradicional TI",
            url="https://jobs.example.com/corp-hist",
            location="Contagem, MG",
            workplace_mode="presencial",
            description=(
                "Nossa empresa possui 15 anos de experiência no mercado mineiro.\n"
                "Estágio remunerado com bolsa auxílio, 08h às 14h.\n"
                "Requisitos: Windows, Active Directory e atendimento Help Desk."
            ),
        )
        res = engine.evaluate(job)
        assert res.hard_incompatibilities == []
        assert res.area_id == "suporte"
        assert res.local_prep_allowed is True

    def test_technical_block_and_login_never_discard_opportunity(self) -> None:
        engine = _load_triage_engine()
        blocked_lead = RawJobInput(
            source="manual:assisted_import",
            title="Estágio em Suporte de TI",
            company="Portal Restrito",
            url="https://br.indeed.com/viewjob?jk=xyz987",
            location="Belo Horizonte, MG",
            description="Estágio em Suporte de TI",
            incomplete_summary=True,
            access_blocked=True,
        )
        res = engine.evaluate(blocked_lead)
        assert res.hard_incompatibilities == []
        assert res.classification != "descartada"
        assert res.service_tier == 3
        assert res.automation_capability == "necessita_intervencao_manual"


class TestSafeAsyncHttpClientSecurity:
    @pytest.mark.anyio
    async def test_robots_cache_captcha_abort_and_budget_enforcement(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            calls.append(url)
            if url == "https://jobs.example.com/robots.txt":
                return httpx.Response(
                    200,
                    text="User-agent: *\nDisallow: /private\nAllow: /\n",
                )
            if url == "https://jobs.example.com/open-1":
                return httpx.Response(200, text="<html><body><h1>Open 1</h1></body></html>")
            if url == "https://jobs.example.com/open-2":
                return httpx.Response(200, text="<html><body><h1>Open 2</h1></body></html>")
            if url == "https://jobs.example.com/private/secret":
                return httpx.Response(200, text="should never be fetched")
            if url == "https://jobs.example.com/captcha":
                return httpx.Response(
                    200,
                    text='<div class="cf-turnstile">Verify you are human</div>',
                )
            if url == "https://jobs.example.com/forbidden":
                return httpx.Response(403, text="Forbidden")
            return httpx.Response(404)

        mock_transport = httpx.MockTransport(handler)
        async with SafeAsyncHttpClient(
            allowed_hosts={"jobs.example.com"},
            max_requests_budget=5,
            default_rate_limit_seconds=0.0,
            transport=mock_transport,
        ) as client:
            # Rejects non-HTTPS and non-allowlisted hosts before making requests
            with pytest.raises(ConnectorError):
                await client.get_text("http://jobs.example.com/open-1")
            with pytest.raises(ConnectorError):
                await client.get_text("https://untrusted.example.org/page")

            t1 = await client.get_text("https://jobs.example.com/open-1")
            t2 = await client.get_text("https://jobs.example.com/open-2")
            assert "Open 1" in t1 and "Open 2" in t2
            # robots.txt must be fetched exactly ONCE across both requests!
            assert calls.count("https://jobs.example.com/robots.txt") == 1

            # Disallowed by robots.txt -> immediate AccessRestrictedError without fetching target
            with pytest.raises(AccessRestrictedError, match="robots.txt"):
                await client.get_text("https://jobs.example.com/private/secret")
            assert "https://jobs.example.com/private/secret" not in calls

            # CAPTCHA signature -> immediate AccessRestrictedError
            with pytest.raises(AccessRestrictedError, match="CAPTCHA"):
                await client.get_text("https://jobs.example.com/captcha")

            # HTTP 403 -> immediate AccessRestrictedError
            with pytest.raises(AccessRestrictedError):
                await client.get_text("https://jobs.example.com/forbidden")

            # Request #6 exceeds max_requests_budget=5 (robots + open1 + open2 + captcha + forbidden = 5)
            with pytest.raises(BudgetExhaustedError):
                await client.get_text("https://jobs.example.com/another")

    @pytest.mark.anyio
    async def test_empregare_mcp_blocks_any_non_readonly_tool(self) -> None:
        mock_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"result": {}}))
        async with SafeAsyncHttpClient(
            allowed_hosts={"www.empregare.com"},
            default_rate_limit_seconds=0.0,
            transport=mock_transport,
        ) as client:
            with pytest.raises(ConnectorError, match="Forbidden MCP"):
                await client.post_empregare_mcp_readonly(
                    "https://www.empregare.com/api/mcp",
                    "tools/call",
                    {"name": "enviar_candidatura", "arguments": {}},
                )


class TestDiscoveryApiConnectorsDeduplicationAndScheduler:
    @pytest.mark.anyio
    async def test_full_discovery_run_assisted_import_deduplication_and_scheduler(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "phase2_workspace"
        settings = AppSettings(
            data_dir=workspace,
            examples_dir=EXAMPLES_DIR,
            scheduler_enabled=False,
            llm_provider="none",
        )

        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>Estágio em Suporte Técnico - Belo Horizonte</title>
            <link>https://jobs.example.com/vaga-suporte-1</link>
            <pubDate>Tue, 17 Nov 2026 12:00:00 GMT</pubDate>
          </item>
          <item>
            <title>Arquiteto de Software Sênior</title>
            <link>https://jobs.example.com/vaga-senior-ignored</link>
          </item>
        </channel></rss>
        """

        html_job_suporte = """
        <html>
          <head>
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@type": "JobPosting",
              "title": "Estágio em Suporte Técnico",
              "hiringOrganization": {"name": "Clínica Mineira"},
              "jobLocation": {"address": {"addressLocality": "Belo Horizonte", "addressRegion": "MG"}},
              "datePosted": "2026-11-17",
              "description": "<p>Estágio remunerado com bolsa R$ 1.500, horário 08h às 14h.</p><p>Requisitos: Windows, Active Directory, Help Desk e GLPI.</p><p>Envie seu currículo em PDF para rh@clinicamineira.example.com</p>"
            }
            </script>
          </head>
          <body><h1>Estágio em Suporte Técnico</h1></body>
        </html>
        """

        gh_payload = {
            "jobs": [
                {
                    "id": 9001,
                    "title": "Estágio em Desenvolvimento Java",
                    "absolute_url": "https://job-boards.greenhouse.io/exampleboard/jobs/9001",
                    "location": {"name": "Belo Horizonte, MG"},
                    "first_published": "2026-11-16T10:00:00Z",
                    "content": "<p>Estágio remunerado com bolsa. Horário comercial 09h às 16h.</p><p>Requisitos: Java, Spring Boot, SQL e Git.</p>",
                }
            ]
        }

        def mock_router(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://jobs.example.com/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            if url == "https://jobs.example.com/feed":
                return httpx.Response(200, text=rss_xml)
            if url == "https://jobs.example.com/vaga-suporte-1":
                return httpx.Response(200, text=html_job_suporte)
            if url == "https://jobs.example.com/blocked-seed":
                return httpx.Response(403, text="Blocked by WAF")
            if url.startswith("https://boards-api.greenhouse.io/v1/boards/exampleboard/jobs"):
                return httpx.Response(
                    200,
                    json=gh_payload,
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(404)

        app = create_app(settings=settings, http_transport=httpx.MockTransport(mock_router))

        async with _asgi_client(app) as client:
            # Enable demo-local-rss AND add a blocked seed page to test Tier 3 lead preservation
            sources_data = (await client.get("/api/sources")).json()
            for s in sources_data["sources"]:
                if s["id"] == "demo-local-rss":
                    s["enabled"] = True
                    s["seed_pages"] = [
                        {
                            "url": "https://jobs.example.com/blocked-seed",
                            "title": "Auxiliar de TI Júnior",
                            "company": "Portal WAF",
                            "location": "Belo Horizonte, MG",
                        }
                    ]
            settings.sources_path.write_text(
                json.dumps(sources_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # 1. Execute on-demand discovery run
            run_res = await client.post("/api/discovery/run", json={"max_requests_budget": 30})
            assert run_res.status_code == 200
            summary = run_res.json()
            assert summary["extracted_count"] == 3  # RSS job + blocked seed lead + Greenhouse job
            assert summary["new_count"] == 3

            # 2. Query jobs & inspect blocked lead vs full jobs
            jobs_list = (await client.get("/api/jobs")).json()
            assert jobs_list["total"] == 3
            by_title = {item["title"]: item for item in jobs_list["items"]}

            sup_job = by_title["Estágio em Suporte Técnico"]
            assert sup_job["area_id"] == "suporte"
            assert sup_job["classification"] == "aprovada_com_preparacao"  # GLPI is preparable_gap!
            assert sup_job["service_tier"] == 1
            assert sup_job["application_method"] == "A"

            dev_job = by_title["Estágio em Desenvolvimento Java"]
            assert dev_job["area_id"] == "dev"
            assert dev_job["classification"] == "aprovada"

            blocked_job = by_title["Auxiliar de TI Júnior"]
            assert blocked_job["classification"] != "descartada"
            assert blocked_job["service_tier"] == 3

            # Advance dev_job stage to simulate user preparing resume, then re-import with UTM params!
            with Session(app.state.engine) as db_sess:
                db_opp = db_sess.get(JobOpportunity, dev_job["id"])
                assert db_opp is not None
                db_opp.application_stage = "curriculo_preparado"
                db_sess.add(db_opp)
                db_sess.commit()

            # 3. Assisted Manual Import with UTM tracking params -> must deduplicate & preserve stage!
            imp_res = await client.post(
                "/api/jobs/import",
                json={
                    "url": "https://job-boards.greenhouse.io/exampleboard/jobs/9001?utm_source=linkedin&gh_src=xyz",
                    "title": "Estágio em Desenvolvimento Java",
                    "company": "Example Tech Corp",
                    "location": "Belo Horizonte, MG",
                    "description": "Estágio remunerado com bolsa. Horário comercial 09h às 16h. Requisitos: Java, Spring Boot, SQL e Git.",
                },
            )
            assert imp_res.status_code == 200
            imp_body = imp_res.json()
            assert imp_body["deduplicated"] is True
            assert imp_body["opportunity_id"] == dev_job["id"]
            assert imp_body["application_stage"] == "curriculo_preparado"

            # 4. Detail endpoint check
            detail = (await client.get(f"/api/jobs/{sup_job['id']}")).json()
            assert "GLPI" in detail["analysis"]["preparable_gaps"]
            assert len(detail["observations"]) >= 1

            # 5. Toggle source endpoint check
            patch_res = await client.patch(
                "/api/sources/demo-local-rss", json={"enabled": False}
            )
            assert patch_res.status_code == 200
            assert patch_res.json()["enabled"] is False

            # 6. APScheduler lifecycle & control endpoints
            sched_initial = (await client.get("/api/scheduler")).json()
            assert sched_initial["scheduler_running"] is True
            assert sched_initial["enabled"] is False
            assert sched_initial["next_run_time"] is None

            sched_on = (
                await client.post(
                    "/api/scheduler",
                    json={"enabled": True, "interval_minutes": 45, "max_requests_budget": 25},
                )
            ).json()
            assert sched_on["enabled"] is True
            assert sched_on["interval_minutes"] == 45
            assert sched_on["next_run_time"] is not None

            sched_off = (await client.post("/api/scheduler", json={"enabled": False})).json()
            assert sched_off["enabled"] is False
            assert sched_off["next_run_time"] is None

    @pytest.mark.anyio
    async def test_sitemap_lever_and_empregare_mcp_connectors_via_mock_transport(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "all_connectors_ws"
        settings = AppSettings(
            data_dir=workspace,
            examples_dir=EXAMPLES_DIR,
            scheduler_enabled=False,
        )

        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://portal.example.com/vaga/assistente-administrativo-belo-horizonte-555</loc>
            <lastmod>2026-11-17</lastmod>
          </url>
        </urlset>
        """

        sitemap_job_html = """
        <html><body>
          <h1 class="job-title">Assistente Administrativo</h1>
          <div class="job-company-name">Escritório Horizonte</div>
          <div class="job-location">Belo Horizonte - MG</div>
          <div class="job-description">
            Vaga CLT, 08h às 17h. Requisitos: Excel, organização de planilhas e atendimento ao cliente.
          </div>
        </body></html>
        """

        lever_json = [
            {
                "id": "lev-777",
                "text": "Estágio em Análise de Dados e BI",
                "hostedUrl": "https://jobs.lever.co/examplelever/lev-777",
                "workplaceType": "remote",
                "categories": {"location": "Brasil", "commitment": "Estágio"},
                "descriptionPlain": "Estágio remunerado com bolsa auxílio, horário diurno flexível.\nRequisitos: SQL, Power BI e Excel.",
            }
        ]

        def mock_router(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            if url == "https://portal.example.com/sitemap.xml":
                return httpx.Response(200, text=sitemap_xml)
            if url == "https://portal.example.com/vaga/assistente-administrativo-belo-horizonte-555":
                return httpx.Response(200, text=sitemap_job_html)
            if url.startswith("https://api.lever.co/v0/postings/examplelever"):
                return httpx.Response(
                    200, json=lever_json, headers={"content-type": "application/json"}
                )
            if url == "https://www.empregare.com/api/mcp" and request.method == "POST":
                body = json.loads(request.content.decode("utf-8"))
                method = body["method"]
                if method == "initialize":
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
                if method == "tools/list":
                    return httpx.Response(
                        200,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {"tools": [{"name": "buscar_vagas"}]},
                        },
                    )
                if method == "tools/call":
                    vagas = {
                        "sucesso": True,
                        "vagas": [
                            {
                                "id": "88001",
                                "cargo": "Auxiliar de Suporte de TI",
                                "empresa": "Tech Gerais",
                                "cidades": ["Belo Horizonte - MG"],
                                "remoto": "Presencial",
                                "urlCandidatura": "https://www.empregare.com/pt-br/vaga-auxiliar-suporte_88001",
                            }
                        ],
                    }
                    return httpx.Response(
                        200,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {
                                "content": [{"type": "text", "text": json.dumps(vagas)}]
                            },
                        },
                    )
            if url == "https://www.empregare.com/pt-br/vaga-auxiliar-suporte_88001":
                return httpx.Response(
                    200,
                    text=(
                        "<html><body><h1>Auxiliar de Suporte de TI</h1>"
                        "<div>Descrição e Responsabilidades: Regime de contratação: CLT. "
                        "Horário comercial 08h às 17h. Requisitos: Windows e redes. "
                        "Código da vaga: 88001</div></body></html>"
                    ),
                )
            return httpx.Response(404)

        app = create_app(settings=settings, http_transport=httpx.MockTransport(mock_router))
        async with _asgi_client(app) as client:
            custom_sources = {
                "schema_version": "1.0",
                "sources": [
                    {
                        "id": "sm-portal",
                        "type": "sitemap_jsonld",
                        "enabled": True,
                        "priority": "high",
                        "sitemaps": ["https://portal.example.com/sitemap.xml"],
                        "allowed_city_slugs": ["belo_horizonte"],
                        "recency_days": 30,
                        "max_items": 5,
                    },
                    {
                        "id": "lev-board",
                        "type": "lever",
                        "enabled": True,
                        "priority": "secondary",
                        "board_token": "examplelever",
                        "company_name": "Example Lever Co",
                        "entry_level_local_only": True,
                        "max_items": 5,
                    },
                    {
                        "id": "emp-mcp",
                        "type": "empregare_mcp",
                        "enabled": True,
                        "priority": "high",
                        "derive_queries_from_catalog": True,
                        "max_queries": 2,
                        "max_items": 5,
                    },
                ],
            }
            settings.sources_path.write_text(
                json.dumps(custom_sources, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            run_res = await client.post("/api/discovery/run", json={"max_requests_budget": 40})
            assert run_res.status_code == 200
            summary = run_res.json()
            assert summary["new_count"] == 3

            jobs = (await client.get("/api/jobs")).json()["items"]
            areas = {j["title"]: j["area_id"] for j in jobs}
            assert areas["Assistente Administrativo"] == "administrativo"
            assert areas["Estágio em Análise de Dados e BI"] == "dados"
            assert areas["Auxiliar de Suporte de TI"] == "suporte"

    @pytest.mark.anyio
    async def test_deduplicates_against_preexisting_migrated_sqlite_fingerprints(
        self, tmp_path: Path
    ) -> None:
        from app.domain.triage_engine import canonicalize_url, compute_job_fingerprint
        from app.models import JobObservation

        workspace = tmp_path / "migrated_dedup_ws"
        settings = AppSettings(data_dir=workspace, examples_dir=EXAMPLES_DIR)
        app = create_app(settings=settings)

        desc = (
            "Vaga CLT 08h às 17h em Belo Horizonte. "
            "Requisitos: Windows, Active Directory e Help Desk."
        )
        fp = compute_job_fingerprint("Horizonte Sistemas", "Analista de Suporte Jr", "Belo Horizonte, MG", desc)
        orig_canon = canonicalize_url("https://carreiras.horizontesistemas.example.com/vaga/42")

        async with _asgi_client(app) as client:
            with Session(app.state.engine) as sess:
                migrated_opp = JobOpportunity(
                    id="legacy-migrated-uuid-042",
                    discovered_at="2026-11-01T10:00:00+00:00",
                    last_seen_at="2026-11-01T10:00:00+00:00",
                    company="Horizonte Sistemas",
                    title="Analista de Suporte Jr",
                    original_url=orig_canon,
                    canonical_url=orig_canon,
                    content_fingerprint=fp,
                    area_id="suporte",
                    classification="aprovada",
                    application_stage="enviada_manualmente",
                )
                sess.add(migrated_opp)
                sess.flush()
                sess.add(
                    JobObservation(
                        opportunity_id=migrated_opp.id,
                        observation_kind="source_sighting",
                        source_name="rss_local:legacy",
                        source_item_id="leg-42",
                        canonical_url=orig_canon,
                        content_fingerprint=fp,
                        observed_at="2026-11-01T10:00:00+00:00",
                    )
                )
                sess.commit()

            # Import same job syndicated on a different URL -> matches content_fingerprint!
            res = await client.post(
                "/api/jobs/import",
                json={
                    "url": "https://agregador.example.org/anuncio/9999",
                    "title": "Analista de Suporte Jr",
                    "company": "Horizonte Sistemas",
                    "location": "Belo Horizonte, MG",
                    "description": desc,
                },
            )
            assert res.status_code == 200
            body = res.json()
            assert body["deduplicated"] is True
            assert body["opportunity_id"] == "legacy-migrated-uuid-042"
            # Must never downgrade 'enviada_manualmente'!
            assert body["application_stage"] == "enviada_manualmente"


