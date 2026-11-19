"""Falhas do Google simuladas: fallback 404 e mensagens legíveis sem vazar chave."""

from __future__ import annotations

import httpx
import pytest

from app.adapters.llm.providers import GeminiAdapter, LlmAdapterError, build_llm_adapter
from app.core.config import AppSettings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ok() -> httpx.Response:
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
        "usageMetadata": {},
    })


@pytest.mark.anyio
async def test_404_moves_to_lite_and_caches_model_in_settings(tmp_path) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if "gemini-2.5-flash:" in str(req.url):
            return httpx.Response(404, json={"error": {"message": "Modelo indisponível"}})
        return _ok()

    settings = AppSettings(data_dir=tmp_path, gemini_api_key="chave-ficticia-comprida")
    adapter = build_llm_adapter(settings, httpx.MockTransport(handler))
    response = await adapter.extract_profile_from_text("Perfil fictício sem dados pessoais.")
    assert response.model == "gemini-2.5-flash-lite"
    assert settings.gemini_model == "gemini-2.5-flash-lite"
    assert len(calls) == 2
    assert all("chave-ficticia" not in url for url in calls)
    await adapter.extract_profile_from_text("Outro perfil fictício.")
    assert calls[-1].endswith("gemini-2.5-flash-lite:generateContent")


@pytest.mark.anyio
async def test_404_chain_reaches_latest_and_validation_uses_fallback() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "gemini-flash-latest" in str(req.url):
            return _ok() if req.method == "POST" else httpx.Response(200, json={"name": "models/gemini-flash-latest"})
        return httpx.Response(404, json={"error": {"message": "Modelo não encontrado"}})

    adapter = GeminiAdapter(api_key="chave-ficticia-comprida", transport=httpx.MockTransport(handler))
    assert await adapter.validate_connection() == "gemini-flash-latest"
    assert (await adapter.extract_profile_from_text("Texto fictício.")).model == "gemini-flash-latest"


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 403, 429])
async def test_google_error_message_is_localized_and_secret_redacted(status: int) -> None:
    secret = "chave-ficticia-comprida"
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"Limite ou acesso inválido: {secret}"}})
    adapter = GeminiAdapter(api_key=secret, transport=httpx.MockTransport(handler))
    with pytest.raises(LlmAdapterError) as captured:
        await adapter.extract_profile_from_text("Texto fictício.")
    assert "Não foi possível acessar o Google" in str(captured.value)
    assert "Limite ou acesso inválido" in str(captured.value)
    assert secret not in str(captured.value)
