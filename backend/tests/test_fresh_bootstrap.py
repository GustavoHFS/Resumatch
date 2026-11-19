"""Primeiro arranque isolado e idempotência sem tocar em user_data real."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.core.config import AppSettings
from app.main import create_app

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_fresh_bootstrap_creates_neutral_workspace_and_preserves_existing_files(tmp_path: Path) -> None:
    workspace = tmp_path / "novo_usuario" / "user_data"
    settings = AppSettings(data_dir=workspace, examples_dir=EXAMPLES)
    assert not workspace.exists()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        for name in ("config", "profile", "db", "exports"):
            assert (workspace / name).is_dir()
        assert settings.db_path.is_file()
        assert json.loads(settings.profile_path.read_text(encoding="utf-8"))["header"]["full_name"] == ""
        assert json.loads(settings.blocked_companies_path.read_text(encoding="utf-8")) == {"blocked_companies": []}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/profile")).json()["header"]["full_name"] == ""
            assert (await client.get("/api/config/blocked")).json()["companies"] == []

    settings.profile_path.write_text('{"custom":"intocado"}', encoding="utf-8")
    settings.criteria_path.write_text('{"custom":"intocado"}', encoding="utf-8")
    app_again = create_app(settings)
    async with app_again.router.lifespan_context(app_again):
        assert settings.profile_path.read_text(encoding="utf-8") == '{"custom":"intocado"}'
        assert settings.criteria_path.read_text(encoding="utf-8") == '{"custom":"intocado"}'
