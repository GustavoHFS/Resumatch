"""Phase 1 test suite: FastAPI lifespan, SQLite WAL, SQLModel entities, and core endpoints.

Strictly uses isolated `tmp_path` workspaces bootstrapped from public `examples/` fixtures
(zero access to private `user_data/`) and `httpx.ASGITransport` (zero background threads
or deprecation warnings).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.config import AppSettings, assert_safe_path
from app.main import create_app
from app.models import (
    ApplicationEvent,
    ApplicationTracking,
    DiscoveryRun,
    JobObservation,
    JobOpportunity,
    ResumeVersion,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_test_settings(tmp_data_dir: Path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_data_dir,
        examples_dir=EXAMPLES_DIR,
        scheduler_enabled=False,
        gemini_api_key="",
    )


@asynccontextmanager
async def _asgi_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


class TestPhase1CoreApiAndWalDatabase:
    @pytest.mark.anyio
    async def test_lifespan_bootstraps_demo_and_enables_sqlite_wal_mode(
        self, tmp_path: Path
    ) -> None:
        settings = _make_test_settings(tmp_path / "demo_workspace")
        app = create_app(settings)

        async with _asgi_client(app) as client:
            res = await client.get("/api/health")
            assert res.status_code == 200
            body = res.json()
            assert body["status"] == "ok"
            assert body["project"] == "Resumatch"
            assert body["sqlite_journal_mode"] == "wal"
            assert body["bootstrapped_demo"] is True
            assert body["counts"] == {
                "opportunities": 0,
                "resume_versions": 0,
                "tracked_applications": 0,
                "discovery_runs": 0,
            }
            assert settings.db_path.is_file()

    @pytest.mark.anyio
    async def test_profile_and_criteria_endpoints_serve_validated_examples(
        self, tmp_path: Path
    ) -> None:
        settings = _make_test_settings(tmp_path / "demo_workspace")
        app = create_app(settings)

        async with _asgi_client(app) as client:
            prof_res = await client.get("/api/profile")
            assert prof_res.status_code == 200
            profile = prof_res.json()
            assert profile["schema_version"] == "2.1"
            assert profile["header"]["full_name"] == "Marina Alves Costa"
            assert len(profile["experiences"]) == 1
            assert "Help Desk" in profile["experiences"][0]["description"]
            assert "Java" in profile["skills"]

            crit_res = await client.get("/api/config/criteria")
            assert crit_res.status_code == 200
            criteria = crit_res.json()
            assert criteria["career_focus"]["max_mandatory_experience_months"] == 24
            assert criteria["omissions_policy"]["allow_local_prep_with_operational_omissions"] is True

            src_res = await client.get("/api/config/sources")
            assert src_res.status_code == 200
            assert len(src_res.json()["sources"]) >= 25

    @pytest.mark.anyio
    async def test_profile_endpoint_returns_422_on_corrupted_schema(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "corrupt_workspace"
        settings = _make_test_settings(data_dir)
        app = create_app(settings)

        async with _asgi_client(app) as client:
            settings.profile_path.write_text('{"schema_version": "1.0"}', encoding="utf-8")
            res = await client.get("/api/profile")
            assert res.status_code == 422

    @pytest.mark.anyio
    async def test_sqlmodel_entities_persist_and_enforce_foreign_keys(
        self, tmp_path: Path
    ) -> None:
        settings = _make_test_settings(tmp_path / "relational_workspace")
        app = create_app(settings)

        async with _asgi_client(app) as client:
            engine = app.state.engine
            with Session(engine) as session:
                opp = JobOpportunity(
                    id="opp-demo-001",
                    discovered_at="2026-11-18T00:00:00+00:00",
                    last_seen_at="2026-11-18T00:00:00+00:00",
                    company="Horizonte Tech",
                    title="Estágio em Desenvolvimento Java",
                    original_url="https://jobs.example.com/java-intern?utm_source=feed",
                    canonical_url="https://jobs.example.com/java-intern",
                    content_fingerprint="fp_demo_001",
                    area_id="dev",
                    classification="aprovada",
                    validity_status="confirmadamente_aberta",
                    application_method="A",
                    application_effort="baixo",
                    service_tier=1,
                )
                session.add(opp)
                session.flush()

                obs = JobObservation(
                    opportunity_id=opp.id,
                    observation_kind="source_sighting",
                    source_name="rss:demo",
                    source_item_id="001",
                    canonical_url=opp.canonical_url,
                    content_fingerprint=opp.content_fingerprint,
                    observed_at="2026-11-18T00:00:00+00:00",
                )
                version = ResumeVersion(
                    id="ver-demo-001",
                    created_at="2026-11-18T00:05:00+00:00",
                    opportunity_id=opp.id,
                    company=opp.company,
                    job_title=opp.title,
                    area_id="dev",
                    generation_mode="deterministic",
                    status="preparada_nao_enviada",
                    description_sha256="abc123sha",
                    pdf_sha256="pdf456sha",
                )
                session.add(obs)
                session.add(version)
                session.flush()

                tracking = ApplicationTracking(
                    opportunity_id=opp.id,
                    updated_at="2026-11-18T00:05:00+00:00",
                    stage="curriculo_preparado",
                    queue_state="candidatura_a_preparar",
                    service_tier=1,
                    resume_version_id=version.id,
                )
                ev = ApplicationEvent(
                    opportunity_id=opp.id,
                    created_at="2026-11-18T00:05:00+00:00",
                    event_source="tracking",
                    stage="curriculo_preparado",
                    notes="Demo resume version linked",
                )
                run = DiscoveryRun(
                    id="run-demo-001",
                    run_kind="discovery",
                    started_at="2026-11-18T00:00:00+00:00",
                    finished_at="2026-11-18T00:00:02+00:00",
                    extracted_count=1,
                    unique_count=1,
                    new_count=1,
                )
                session.add(tracking)
                session.add(ev)
                session.add(run)
                session.commit()

            health = (await client.get("/api/health")).json()
            assert health["counts"] == {
                "opportunities": 1,
                "resume_versions": 1,
                "tracked_applications": 1,
                "discovery_runs": 1,
            }

            # Foreign key constraint check: orphan event must fail
            with Session(engine) as bad_session:
                bad_session.add(
                    ApplicationEvent(
                        opportunity_id="non-existent-opportunity",
                        created_at="2026-11-18T00:10:00+00:00",
                        stage="descoberta",
                    )
                )
                with pytest.raises(IntegrityError):
                    bad_session.commit()

    @pytest.mark.parametrize("forbidden_segment", ["Dados Sensíveis", "Dados Sensiveis", "vault"])
    def test_vault_path_guard_blocks_prohibited_directories(
        self, tmp_path: Path, forbidden_segment: str
    ) -> None:
        blocked_path = tmp_path / forbidden_segment / "secrets.json"
        with pytest.raises(PermissionError, match="strictly prohibited"):
            assert_safe_path(blocked_path)
