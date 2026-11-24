"""Falhas do Google simuladas: fallback 404 e mensagens legíveis sem vazar chave."""

from __future__ import annotations

import httpx
import pytest
import json

from app.adapters.llm.providers import GeminiAdapter, LlmAdapterError, build_llm_adapter
from app.core.config import AppSettings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _no_sleep(_seconds: float) -> None:
    pass


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

    settings = AppSettings(data_dir=tmp_path, gemini_api_key="chave-ficticia-comprida", gemini_model="gemini-2.5-flash")
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
        if req.url.path == "/v1beta/models":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]}]})
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
    adapter = GeminiAdapter(api_key=secret, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    with pytest.raises(LlmAdapterError) as captured:
        await adapter.extract_profile_from_text("Texto fictício.")
    assert "Não foi possível acessar o Google" in str(captured.value)
    assert "Limite ou acesso inválido" in str(captured.value)
    assert secret not in str(captured.value)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [429, 500, 502, 503, 504, "timeout"])
async def test_transient_failure_tries_next_model_and_caches_success(failure) -> None:
    calls: list[str] = []
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(req: httpx.Request) -> httpx.Response:
        model = req.url.path.split("/")[-1].split(":")[0]
        calls.append(model)
        if model == "gemini-2.5-flash-lite":
            if failure == "timeout":
                raise httpx.ReadTimeout("simulated", request=req)
            return httpx.Response(failure, json={"error": {"message": "Tente novamente"}})
        return _ok()

    adapter = GeminiAdapter(api_key="fake-test-key", transport=httpx.MockTransport(handler), sleep=sleep)
    result = await adapter.extract_profile_from_text("Perfil fictício.")
    assert result.model == "gemini-2.5-flash"
    assert calls == ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
    assert delays == [1.5]
    await adapter.extract_profile_from_text("Outro perfil.")
    assert calls[-1] == "gemini-2.5-flash"


@pytest.mark.anyio
async def test_thinking_config_rejection_retries_once_without_field() -> None:
    configs: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        config = json.loads(req.content)["generationConfig"]
        configs.append(config)
        if "thinkingConfig" in config:
            return httpx.Response(400, json={"error": {"message": "Unknown name thinkingConfig"}})
        return _ok()

    adapter = GeminiAdapter(api_key="fake-test-key", transport=httpx.MockTransport(handler))
    assert (await adapter.extract_profile_from_text("Perfil.")).model == "gemini-2.5-flash-lite"
    assert configs[0]["thinkingConfig"] == {"thinkingBudget": 0}
    assert "thinkingConfig" not in configs[1]
    await adapter.extract_profile_from_text("Outro perfil.")
    assert len(configs) == 3 and "thinkingConfig" not in configs[-1]


@pytest.mark.anyio
async def test_catalog_discovery_filters_non_text_models_and_uses_header_key() -> None:
    calls: list[str] = []
    candidates = [
        "gemini-embedding", "imagen-generate", "veo-generate", "gemini-tts", "gemini-robotics",
        "gemini-computer-use", "gemini-new-text", "gemini-new-text", "gemini-no-method",
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        assert "fake-test-key" not in str(req.url)
        assert req.headers["x-goog-api-key"] == "fake-test-key"
        if req.method == "GET":
            return httpx.Response(200, json={"models": [
                {"name": f"models/{model}", "supportedGenerationMethods": [] if model == "gemini-no-method" else ["generateContent"]}
                for model in candidates
            ]})
        if "gemini-new-text:" in req.url.path:
            assert "thinkingConfig" not in json.loads(req.content)["generationConfig"]
            return _ok()
        return httpx.Response(404, json={"error": {"message": "Modelo não encontrado"}})

    adapter = GeminiAdapter(api_key="fake-test-key", transport=httpx.MockTransport(handler))
    result = await adapter.extract_profile_from_text("Perfil.")
    assert result.model == "gemini-new-text"
    assert len(calls) == 6  # 4 default models, catalog, 1 valid discovered model


@pytest.mark.anyio
async def test_retry_round_happens_after_other_models_with_capped_backoff() -> None:
    calls: list[str] = []
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"models": []})
        calls.append(req.url.path)
        if len(calls) == 5:
            return _ok()
        return httpx.Response(503, json={"error": {"message": "Overloaded"}})

    adapter = GeminiAdapter(api_key="fake-test-key", transport=httpx.MockTransport(handler), sleep=sleep)
    await adapter.extract_profile_from_text("Perfil.")
    assert calls[0] == calls[-1]
    assert len(set(calls[:4])) == 4
    assert delays == [1.5, 3.0, 3.0, 3.0]


@pytest.mark.anyio
async def test_whole_fallback_respects_total_deadline() -> None:
    import asyncio

    async def handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(.1)
        return _ok()

    adapter = GeminiAdapter(api_key="fake-test-key", total_timeout_seconds=.01,
                            transport=httpx.MockTransport(handler))
    with pytest.raises(LlmAdapterError, match="dentro do prazo"):
        await adapter.extract_profile_from_text("Perfil.")


@pytest.mark.anyio
async def test_catalog_failure_does_not_prevent_retry_of_known_model() -> None:
    posts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal posts
        if req.method == "GET":
            return httpx.Response(403, json={"error": {"message": "ListModels indisponível"}})
        posts += 1
        return _ok() if posts == 5 else httpx.Response(503)

    adapter = GeminiAdapter(api_key="fake-test-key", transport=httpx.MockTransport(handler), sleep=_no_sleep)
    result = await adapter.extract_profile_from_text("Perfil.")
    assert result.model == "gemini-2.5-flash-lite"
    assert posts == 5


def test_job_payload_is_small_and_keeps_intro_requirements_and_pii_masking() -> None:
    from app.adapters.llm.pii_stripper import strip_pii_for_resume_generation
    from app.models import MasterProfile, BlockedCompaniesConfig

    profile = MasterProfile(header={"full_name": "Pessoa Sintética", "email": "demo@example.com"})
    description = "<h1>Suporte técnico júnior</h1><p>" + "Texto comercial irrelevante. " * 700 + "</p>"
    description += "<h2>Requisitos</h2><ul><li>Python, SQL e Git.</li><li>Contato demo@example.com</li></ul>"
    context = strip_pii_for_resume_generation(profile=profile, blocked=BlockedCompaniesConfig(),
                                              job_description=description)
    outbound = context.sanitized.target_job_description
    assert len(outbound) <= 4500
    assert outbound.startswith("Suporte técnico júnior")
    assert "Requisitos" in outbound and "Python, SQL e Git" in outbound
    assert "demo@example.com" not in outbound
    assert "<li>" not in outbound
