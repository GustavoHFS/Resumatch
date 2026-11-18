"""Phase 5 test suite: Single-process SPA static serving, safe LLM config API, and PDF download.

Validates via synchronous in-memory `httpx.ASGITransport` (zero background uvicorn processes,
zero access to real `user_data/`).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.core.config import AppSettings
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"


class _FakePdfRenderer:
    async def render_html_to_pdf(self, html_content: str, timeout_ms: int = 10_000) -> bytes:
        from pypdf import PdfWriter
        from pypdf.generic import (
            ArrayObject,
            DictionaryObject,
            FloatObject,
            NameObject,
            TextStringObject,
        )
        import io

        writer = PdfWriter()
        page = writer.add_blank_page(width=595, height=842)
        # Minimal clickable link annotation + text stream so ATS validator passes
        annot = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject(
                    [FloatObject(10), FloatObject(10), FloatObject(100), FloatObject(30)]
                ),
                NameObject("/A"): DictionaryObject(
                    {
                        NameObject("/S"): NameObject("/URI"),
                        NameObject("/URI"): TextStringObject("https://github.com/marina-costa-demo"),
                    }
                ),
            }
        )
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annot)])
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()


def _make_phase5_settings(tmp_path: Path, dist_dir: Path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "workspace_data",
        examples_dir=EXAMPLES_DIR,
        frontend_dist_dir=dist_dir,
        scheduler_enabled=False,
        gemini_api_key="",
    )


class TestPhase5SpaServingAndConfigEndpoints:
    @pytest.mark.anyio
    async def test_spa_static_bundle_and_client_fallback_preserve_api_and_docs_priority(
        self, tmp_path: Path
    ) -> None:
        dist_dir = tmp_path / "dist"
        assets_dir = dist_dir / "assets"
        assets_dir.mkdir(parents=True)
        (dist_dir / "index.html").write_text(
            "<!doctype html><html lang='pt-BR'><body><div id='root'>Resumatch SPA</div></body></html>",
            encoding="utf-8",
        )
        (assets_dir / "app-bundle.js").write_text(
            "console.log('resumatch-spa-ready');",
            encoding="utf-8",
        )

        settings = _make_phase5_settings(tmp_path, dist_dir)
        app = create_app(settings)

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client:
                # 1. Root serves index.html
                root_res = await client.get("/")
                assert root_res.status_code == 200
                assert "Resumatch SPA" in root_res.text

                # 2. Static JS asset in /assets/* is served directly
                asset_res = await client.get("/assets/app-bundle.js")
                assert asset_res.status_code == 200
                assert "resumatch-spa-ready" in asset_res.text

                # 3. Deep client route falls back to index.html for SPA router
                fallback_res = await client.get("/estudio/curriculo")
                assert fallback_res.status_code == 200
                assert "Resumatch SPA" in fallback_res.text

                # 4. /api/* and /docs retain strict priority (unknown /api/* returns 404 JSON, never HTML)
                health_res = await client.get("/api/health")
                assert health_res.status_code == 200
                assert health_res.json()["project"] == "Resumatch"

                missing_api = await client.get("/api/non-existent-route")
                assert missing_api.status_code == 404
                assert missing_api.json()["detail"] == "API endpoint not found."

                docs_res = await client.get("/docs")
                assert docs_res.status_code == 200
                assert "swagger" in docs_res.text.lower()

    @pytest.mark.anyio
    async def test_guided_google_connect_validates_in_real_time_masks_key_and_persists_atomically(
        self, tmp_path: Path
    ) -> None:
        dist_dir = tmp_path / "empty_dist"
        settings = _make_phase5_settings(tmp_path, dist_dir)
        synthetic_key = "test_secret_" + "key_value_999"

        def google_verify_mock(req: httpx.Request) -> httpx.Response:
            sent_key = req.headers.get("x-goog-api-key")
            if sent_key == synthetic_key:
                return httpx.Response(200, json={"name": "models/gemini-2.5-flash"})
            return httpx.Response(400, json={"error": {"message": "Invalid key"}})

        app = create_app(settings, llm_transport=httpx.MockTransport(google_verify_mock))

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client:
                initial = await client.get("/api/config/llm")
                assert initial.status_code == 200
                assert initial.json()["connected"] is False

                # Rejects invalid Google key in real time
                bad_res = await client.post(
                    "/api/config/llm/connect",
                    json={"api_key": "wrong_key_value_00000"},
                )
                assert bad_res.status_code == 400

                # Validates & activates valid Google key immediately, masking raw secret in response
                connected = await client.post(
                    "/api/config/llm/connect",
                    json={"api_key": synthetic_key},
                )
                assert connected.status_code == 200
                body = connected.json()
                assert body["status"] == "connected"
                assert body["llm"]["connected"] is True
                assert synthetic_key not in connected.text

                health = await client.get("/api/health")
                assert health.json()["ai_connected"] is True
                assert synthetic_key not in health.text

                # Disconnect clears saved key cleanly
                disc = await client.delete("/api/config/llm")
                assert disc.status_code == 200
                assert disc.json()["llm"]["connected"] is False

    @pytest.mark.anyio
    async def test_version_pdf_download_endpoint_serves_exported_file(
        self, tmp_path: Path
    ) -> None:
        from app.models import ResumeVersion
        from sqlmodel import Session

        dist_dir = tmp_path / "empty_dist"
        settings = _make_phase5_settings(tmp_path, dist_dir)
        app = create_app(settings)

        async with app.router.lifespan_context(app):
            export_file = settings.exports_dir / "dev" / "Marina Alves Costa - Dev.pdf"
            export_file.parent.mkdir(parents=True, exist_ok=True)
            sample_pdf_bytes = b"%PDF-1.4\n%Resumatch-Phase5-Download-Test\n%%EOF\n"
            export_file.write_bytes(sample_pdf_bytes)

            with Session(app.state.engine) as session:
                session.add(
                    ResumeVersion(
                        id="ver_phase5_dl_01",
                        created_at="2026-11-18T05:00:00+00:00",
                        area_id="dev",
                        generation_mode="deterministic",
                        status="exported",
                        description_sha256="b" * 64,
                        pdf_sha256="a" * 64,
                        export_rel_path="exports/dev/Marina Alves Costa - Dev.pdf",
                    )
                )
                session.commit()

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client:
                dl_res = await client.get("/api/resumes/versions/ver_phase5_dl_01/pdf")
                assert dl_res.status_code == 200
                assert dl_res.headers["content-type"] == "application/pdf"
                assert dl_res.content == sample_pdf_bytes

                missing_res = await client.get("/api/resumes/versions/unknown_ver/pdf")
                assert missing_res.status_code == 404

    @pytest.mark.anyio
    async def test_real_vite_production_dist_served_end_to_end_when_built(
        self, tmp_path: Path
    ) -> None:
        import re

        real_dist = REPO_ROOT / "frontend" / "dist"
        if not (real_dist / "index.html").is_file():
            pytest.skip("frontend/dist not built yet")

        settings = _make_phase5_settings(tmp_path, real_dist)
        app = create_app(settings)

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client:
                index_res = await client.get("/")
                assert index_res.status_code == 200
                assert "Resumatch" in index_res.text

                js_match = re.search(r'src="(/assets/[^"]+\.js)"', index_res.text)
                css_match = re.search(r'href="(/assets/[^"]+\.css)"', index_res.text)
                assert js_match is not None
                assert css_match is not None

                js_res = await client.get(js_match.group(1))
                assert js_res.status_code == 200
                assert len(js_res.content) > 1000

                css_res = await client.get(css_match.group(1))
                assert css_res.status_code == 200
                assert "--bg-main" in css_res.text

    @pytest.mark.anyio
    async def test_lifecycle_heartbeat_persisted_scheduler_and_atomic_source_toggle(
        self, tmp_path: Path
    ) -> None:
        dist_dir = tmp_path / "empty_dist"
        settings = _make_phase5_settings(tmp_path, dist_dir)
        app1 = create_app(settings)

        async with app1.router.lifespan_context(app1):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app1), base_url="http://127.0.0.1:8000"
            ) as client:
                # 1. Lifecycle ping/status/close contract for Startup-Boost-safe launcher
                s0 = (await client.get("/api/lifecycle/status")).json()
                assert s0["active_clients"] == 0
                assert s0["ever_connected"] is False

                ping = await client.post("/api/lifecycle/ping", json={"client_id": "win_tab_1"})
                assert ping.status_code == 200
                assert ping.json()["active_clients"] == 1

                s1 = (await client.get("/api/lifecycle/status")).json()
                assert s1["active_clients"] == 1
                assert s1["ever_connected"] is True

                closed = await client.post("/api/lifecycle/close", json={"client_id": "win_tab_1"})
                assert closed.status_code == 200
                s2 = (await client.get("/api/lifecycle/status")).json()
                assert s2["active_clients"] == 0
                assert s2["closed_signal"] is True

                # 2. Scheduler interval persists to disk across app restarts
                sched = await client.post(
                    "/api/scheduler",
                    json={"enabled": False, "interval_minutes": 120, "max_requests_budget": 55},
                )
                assert sched.status_code == 200
                assert settings.scheduler_config_path.is_file()

                # 3. Source toggle uses atomic validated write + backup
                srcs = (await client.get("/api/sources")).json()["sources"]
                first_id = srcs[0]["id"]
                toggled = await client.patch(f"/api/sources/{first_id}", json={"enabled": False})
                assert toggled.status_code == 200
                assert toggled.json()["enabled"] is False
                assert toggled.json()["backup_snapshot"] is not None

        # Fresh app instance reloads persisted scheduler interval from scheduler.json
        app2 = create_app(settings)
        async with app2.router.lifespan_context(app2):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app2), base_url="http://127.0.0.1:8000"
            ) as client2:
                reloaded_sched = (await client2.get("/api/scheduler")).json()
                assert reloaded_sched["interval_minutes"] == 120
                assert reloaded_sched["max_requests_budget"] == 55


