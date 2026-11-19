"""Busca Gupy pública dinâmica e atribuição da origem após deduplicação."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import Session

from app.api.jobs_routes import build_source_label
from app.connectors.sources import GupyApiConnector
from app.core.config import AppSettings
from app.core.database import create_sqlite_wal_engine, init_db_schema
from app.domain.triage_engine import DeterministicTriageEngine, RawJobInput
from app.models import BlockedCompaniesConfig, MasterProfile, SearchCriteria
from app.models.sources_schema import GupyApiSourceConfig
from app.services.discovery_service import upsert_and_triage_job

EXAMPLES = Path(__file__).resolve().parent / "fixtures" / "demo_examples"


def _criteria(**changes: object) -> SearchCriteria:
    raw = json.loads((EXAMPLES / "search_criteria.example.json").read_text(encoding="utf-8"))
    return SearchCriteria.model_validate({**raw, **changes})


def _source() -> GupyApiSourceConfig:
    return GupyApiSourceConfig(id="portal_publico", type="gupy_api", priority="high")


def test_dynamic_public_urls_for_regional_remote_and_neutral_preferences() -> None:
    connector = GupyApiConnector(_source(), None, _criteria(
        target_state="Pernambuco", target_cities=["Recife", "Jaboatão dos Guararapes"],
    ))
    urls = connector._public_search_urls()
    assert urls[:3] == [
        "https://portal.gupy.io/job-search/sortBy=publishedDate&state=Pernambuco"
        "&city[]=Recife%2CJaboat%C3%A3o%20dos%20Guararapes?page=" + str(page)
        for page in (1, 2, 3)
    ]
    assert len(urls) == 5
    assert "workplaceType[]=remote?page=2" in urls[-1]
    neutral = GupyApiConnector(_source(), None, _criteria(
        target_state="", target_cities=[], include_remote=False,
    ))._public_search_urls()
    assert neutral == [
        f"https://portal.gupy.io/job-search/sortBy=publishedDate?page={page}"
        for page in (1, 2, 3)
    ]


@pytest.mark.anyio
async def test_nested_next_data_short_card_keeps_tech_job_without_admin_role() -> None:
    job = {
        "id": 42, "name": "Analista de Suporte Júnior", "careerPageName": "Empresa Exemplo",
        "jobUrl": "https://empresa-exemplo.gupy.io/jobs/42", "city": "Recife",
        "state": "Pernambuco", "publishedDate": datetime.now(timezone.utc).isoformat(),
        "description": "Python e SQL",
    }
    admin = {**job, "id": 43, "name": "Auxiliar Administrativo", "jobUrl": "https://empresa-exemplo.gupy.io/jobs/43"}
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({
        "props": {"pageProps": {"dehydratedState": {"queries": [
            {"state": {"data": {"jobs": {"data": [job, admin]}}}}
        ]}}}
    }) + "</script>"

    class Client:
        async def get_text(self, url: str, **_kwargs: object) -> str:
            return html if "job-search" in url else ""

    connector = GupyApiConnector(_source(), Client(), _criteria(
        target_state="Pernambuco", target_cities=["Recife"], include_remote=False,
    ))
    found, errors, stats = await connector.discover()
    assert not errors
    assert stats["gupy_candidates"] == 2
    assert len(found) == 1
    assert found[0].source.startswith("gupy_api:")
    assert found[0].incomplete_summary


def test_gupy_source_wins_when_cross_portal_matches(tmp_path: Path) -> None:
    criteria = _criteria()
    profile = MasterProfile.model_validate_json((EXAMPLES / "master_profile.example.json").read_text(encoding="utf-8"))
    blocked = BlockedCompaniesConfig.model_validate_json((EXAMPLES / "blocked_companies.example.json").read_text(encoding="utf-8"))
    engine = DeterministicTriageEngine(criteria=criteria, blocked=blocked, profile=profile)
    engine_db = create_sqlite_wal_engine(tmp_path / "test.sqlite3")
    init_db_schema(engine_db)
    try:
        with Session(engine_db) as session:
            common = dict(title="Analista de Suporte Júnior", company="Empresa Exemplo",
                          location="Recife, PE", description="Python, SQL e suporte técnico em redes.")
            linkedin = RawJobInput(source="html_board:linkedin", source_id="li-42",
                                   url="https://www.linkedin.com/jobs/view/42", **common)
            gupy = RawJobInput(source="gupy_api:portal", source_id="g-42",
                               url="https://empresa-exemplo.gupy.io/jobs/42", **common)
            first, _, created = upsert_and_triage_job(session, engine, linkedin)
            assert created
            second, _, created = upsert_and_triage_job(session, engine, gupy)
            assert not created and second.id == first.id
            assert second.original_url == gupy.url
            assert build_source_label(second.primary_source, second.original_url) == "Gupy"
    finally:
        engine_db.dispose()
