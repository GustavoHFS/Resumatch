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
        llm_provider="none",
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
                fallback_res = await client.get("/studio/suporte-en")
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
    async def test_llm_config_endpoint_masks_api_keys_and_persists_atomically(
        self, tmp_path: Path
    ) -> None:
        dist_dir = tmp_path / "empty_dist"
        settings = _make_phase5_settings(tmp_path, dist_dir)
        app = create_app(settings)

        synthetic_key = "test_secret_" + "key_value_999"

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client:
                initial = await client.get("/api/config/llm")
                assert initial.status_code == 200
                assert initial.json()["provider"] == "none"
                assert initial.json()["gemini_key_configured"] is False

                updated = await client.put(
                    "/api/config/llm",
                    json={
                        "provider": "gemini",
                        "timeout_seconds": 18.0,
                        "gemini_model": "gemini-2.5-flash",
                        "gemini_api_key": synthetic_key,
                        "groq_model": "llama-3.3-70b-versatile",
                        "ollama_base_url": "http://127.0.0.1:11434",
                        "ollama_model": "llama3.2",
                    },
                )
                assert updated.status_code == 200
                body = updated.json()
                assert body["llm"]["provider"] == "gemini"
                assert body["llm"]["gemini_key_configured"] is True
                # Raw secret key must NEVER appear anywhere in the HTTP response
                assert synthetic_key not in updated.text

                get_again = await client.get("/api/config/llm")
                assert get_again.status_code == 200
                assert get_again.json()["gemini_key_configured"] is True
                assert synthetic_key not in get_again.text

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
                assert "--bg-canvas" in css_res.text

