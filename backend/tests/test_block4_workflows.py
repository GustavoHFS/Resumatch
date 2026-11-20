"""Bloco 4: vagas manuais, reconciliação em gravações e PDF vinculado.

Todos os dados são sintéticos e permanecem em diretórios temporários.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
from sqlmodel import Session

from app.core.config import AppSettings
from app.main import create_app
from app.models import JobOpportunity, MasterProfile, ResumeDraft, build_draft_header_from_profile

EXAMPLES_DIR = Path(__file__).resolve().parent / "fixtures" / "demo_examples"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def client_for(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    app = create_app(AppSettings(data_dir=tmp_path / "workspace", examples_dir=EXAMPLES_DIR))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client, app


@pytest.mark.anyio
async def test_manual_jobs_without_links_or_with_shared_ats_links_never_collide(tmp_path: Path) -> None:
    async with client_for(tmp_path) as (client, app):
        ids: list[str] = []
        for title, url in [
            ("Analista de Dados", ""),
            ("Analista de Dados", ""),
            ("Analista de Suporte", "https://jobs.example.com/carreiras?token=demo-a"),
            ("Desenvolvedora Python", "https://jobs.example.com/carreiras?token=demo-a"),
        ]:
            res = await client.post("/api/jobs/import", json={
                "url": url,
                "title": title,
                "company": "Empresa Fictícia",
                "description": f"Vaga para {title}. Requisitos: Python, SQL e APIs REST.",
            })
            assert res.status_code == 200, res.text
            ids.append(res.json()["opportunity_id"])
        assert len(set(ids)) == 4
        other = await client.post("/api/jobs/import", json={
            "url": "https://jobs.example.com/carreiras?token=demo-a",
            "title": "Analista de Suporte", "company": "Empresa Fictícia",
            "description": "Outra vaga de suporte, com atividades diferentes e SQL.",
        })
        assert other.status_code == 200, other.text
        assert other.json()["opportunity_id"] not in ids
        with Session(app.state.engine) as session:
            first, second = session.get(JobOpportunity, ids[0]), session.get(JobOpportunity, ids[1])
            assert first and second
            assert first.original_url.startswith("https://manual.resumatch.local/vaga/")
            assert first.original_url != second.original_url
            assert session.get(JobOpportunity, ids[2]).title == "Analista de Suporte"
            assert session.get(JobOpportunity, ids[3]).title == "Desenvolvedora Python"

        unsafe = await client.post("/api/jobs/import", json={
            "url": "https://127.0.0.1/vaga?token=demo", "title": "Analista",
            "description": "Descrição fictícia com Python.",
        })
        assert unsafe.status_code == 400


@pytest.mark.anyio
async def test_profile_criteria_and_blocked_writes_reconcile_only_automatic_jobs(tmp_path: Path) -> None:
    async with client_for(tmp_path) as (client, app):
        manual = (await client.post("/api/jobs/import", json={
            "title": "Assistente Administrativo", "description": "Vaga administrativa fictícia."
        })).json()["opportunity_id"]
        with Session(app.state.engine) as session:
            for id_, title in [
                ("auto-tech", "Analista de Suporte Júnior"),
                ("auto-admin", "Auxiliar Administrativo"),
            ]:
                session.add(JobOpportunity(
                    id=id_, discovered_at="2026-11-18T12:00:00+00:00",
                    last_seen_at="2026-11-18T12:00:00+00:00",
                    title=title, company="Empresa Fictícia", primary_source="rss_local:exemplo",
                    source_job_id=id_, original_url=f"https://jobs.example.com/{id_}",
                    canonical_url=f"https://jobs.example.com/{id_}",
                    classification="aprovada", professional_compatibility=None,
                    description_text="Vaga com Python, SQL e suporte técnico.",
                ))
            session.commit()

        for read_path, write_path in [
            ("/api/profile", "/api/profile"),
            ("/api/config/criteria", "/api/config/criteria"),
            ("/api/config/blocked", "/api/config/blocked"),
        ]:
            payload = (await client.get(read_path)).json()
            saved = await client.put(write_path, json=payload)
            assert saved.status_code == 200, saved.text
            with Session(app.state.engine) as session:
                tech = session.get(JobOpportunity, "auto-tech")
                admin = session.get(JobOpportunity, "auto-admin")
                manual_job = session.get(JobOpportunity, manual)
                assert tech and tech.professional_compatibility is not None
                assert admin and admin.classification == "descartada"
                assert manual_job and manual_job.title == "Assistente Administrativo"


@pytest.mark.anyio
async def test_export_without_opportunity_links_immutable_pdf_to_ready_card(tmp_path: Path) -> None:
    async with client_for(tmp_path) as (client, app):
        profile = MasterProfile.model_validate((await client.get("/api/profile")).json())
        draft = ResumeDraft(
            target_role="Analista de Suporte Júnior", company="Empresa Fictícia",
            header=build_draft_header_from_profile(profile.header),
            summary="Profissional com suporte técnico, Windows e Python.",
        )
        payload = {
            "draft": draft.model_dump(mode="json"),
            "manual_job_description": "Vaga fictícia de suporte técnico com Python e SQL.",
        }
        res = await client.post("/api/resumes/export-pdf", json=payload)
        assert res.status_code == 200, res.text
        body = res.json()
        opp_id = body["opportunity_id"]
        assert opp_id and body["job_url"].startswith("https://manual.resumatch.local/vaga/")
        cards = (await client.get("/api/applications")).json()["items"]
        card = next(item for item in cards if item["opportunity_id"] == opp_id)
        assert card["stage"] == "curriculo_preparado"
        assert card["resume_version"]["id"] == body["version_id"]
        assert (app.state.settings.safe_data_dir / body["export_rel_path"]).is_file()

        second = await client.post("/api/resumes/export-pdf", json=payload)
        assert second.status_code == 200, second.text
        assert second.json()["version_id"] != body["version_id"]
        assert second.json()["export_rel_path"] != body["export_rel_path"]
        assert (app.state.settings.safe_data_dir / body["export_rel_path"]).is_file()

        sent = await client.patch(f"/api/applications/{opp_id}/stage", json={
            "stage": "enviada_manualmente", "confirm_manual_submission": True,
        })
        assert sent.status_code == 200, sent.text
        assert sent.json()["user_confirmed_at"]


@pytest.mark.anyio
async def test_all_classifications_visible_with_filters_and_chronological_order(tmp_path: Path) -> None:
    async with client_for(tmp_path) as (client, app):
        with Session(app.state.engine) as session:
            for index, classification in enumerate(["aprovada", "descartada", "fora_foco"]):
                session.add(JobOpportunity(
                    id=f"visibility-{index}", title="Suporte demonstrativo",
                    company="Empresa Fictícia", classification=classification,
                    published_at=f"2026-11-{10 + index}T12:00:00+00:00",
                    discovered_at="2026-11-13T12:00:00+00:00",
                    last_seen_at="2026-11-13T12:00:00+00:00",
                    original_url=f"https://example.com/visibility-{index}",
                    canonical_url=f"https://example.com/visibility-{index}",
                ))
            session.commit()
        response = await client.get("/api/jobs", params={"search": "demonstrativo"})
        assert response.status_code == 200
        assert [item["classification"] for item in response.json()["items"]] == [
            "fora_foco", "descartada", "aprovada",
        ]
        filtered = await client.get("/api/jobs", params={"classification": "descartada"})
        assert all(item["classification"] == "descartada" for item in filtered.json()["items"])


@pytest.mark.anyio
async def test_saved_application_export_links_pdf_without_regressing_sent_stage(tmp_path: Path) -> None:
    async with client_for(tmp_path) as (client, app):
        opp_id = (await client.post("/api/jobs/import", json={
            "title": "Suporte Júnior", "description": "Suporte técnico com Python e SQL.",
        })).json()["opportunity_id"]
        # Assisted manual imports are already tracked; enqueue must remain idempotent.
        for _ in range(2):
            saved = await client.post("/api/applications", json={"opportunity_id": opp_id})
            assert saved.json()["stage"] == "candidatura_a_preparar"
        assert (await client.get("/api/applications")).json()["total"] == 1
        profile = MasterProfile.model_validate((await client.get("/api/profile")).json())
        draft = ResumeDraft(target_role="Suporte Júnior",
                            header=build_draft_header_from_profile(profile.header),
                            summary="Suporte técnico com Python e SQL.")
        payload = {"opportunity_id": opp_id, "draft": draft.model_dump(mode="json")}
        exported = await client.post("/api/resumes/export-pdf", json=payload)
        assert exported.status_code == 200, exported.text
        card = (await client.get("/api/applications")).json()["items"][0]
        assert card["stage"] == "curriculo_preparado"
        assert card["queue_state"] == "curriculo_preparado"
        assert card["resume_version"]["id"] == exported.json()["version_id"]
        assert (await client.get(f"/api/resumes/versions/{card['resume_version']['id']}/pdf")).status_code == 200
        await client.patch(f"/api/applications/{opp_id}/stage", json={
            "stage": "enviada_manualmente", "confirm_manual_submission": True,
        })
        assert (await client.post("/api/resumes/export-pdf", json=payload)).status_code == 200
        assert (await client.get("/api/applications")).json()["items"][0]["stage"] == "enviada_manualmente"
