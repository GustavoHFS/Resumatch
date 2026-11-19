"""Reload and inactivity grace without launching a browser or a server process."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.api import routes
from app.core.config import AppSettings
from app.main import create_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_boot_reload_close_and_inactivity_grace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clock = [1000.0]
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock[0])
    app = create_app(AppSettings(
        data_dir=tmp_path / "workspace",
        examples_dir=Path(__file__).resolve().parents[2] / "examples",
    ))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            async def status() -> dict:
                response = await client.get("/api/lifecycle/status")
                assert response.status_code == 200
                return response.json()

            assert (await status())["boot_grace_seconds"] == 120
            clock[0] += 119
            assert not (await status())["shutdown_ready"]
            await client.post("/api/lifecycle/ping", json={"client_id": "aba"})
            await client.post("/api/lifecycle/close", json={"client_id": "aba"})
            clock[0] += 9
            assert not (await status())["shutdown_ready"]
            await client.post("/api/lifecycle/ping", json={"client_id": "aba"})
            clock[0] += 3
            state = await status()
            assert state["close_requested_at"] is None
            assert not state["shutdown_ready"]
            await client.post("/api/lifecycle/close", json={"client_id": "aba"})
            clock[0] += 10
            assert (await status())["shutdown_ready"]
            await client.post("/api/lifecycle/ping", json={"client_id": "nova-aba"})
            assert not (await status())["shutdown_ready"]
            clock[0] += 1799
            assert not (await status())["shutdown_ready"]
            clock[0] += 2
            assert (await status())["shutdown_ready"]
