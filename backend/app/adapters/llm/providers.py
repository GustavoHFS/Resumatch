"""Lightweight async LLM adapters (`GeminiAdapter`, `GroqAdapter`, `OllamaAdapter`) via `httpx`.

Zero heavy vendor SDKs. Uses structured JSON outputs, explicit short timeouts, OS truststore
TLS verification, and strict credential hygiene (keys never appear in URLs, logs, or errors).
"""

from __future__ import annotations

import json
import ssl
from typing import Any, Literal, Protocol

import httpx
import truststore
from pydantic import Field

from app.adapters.llm.pii_stripper import SanitizedSurgicalInput
from app.core.config import AppSettings
from app.models.profile_schema import StrictModel

ProviderName = Literal["gemini", "groq", "ollama"]


class LlmAdapterError(RuntimeError):
    """Safe error raised on LLM transport, quota, or schema failures (zero secret leakage)."""


class RewrittenBullet(StrictModel):
    bullet_id: str
    text: str


class SurgicalLlmProposal(StrictModel):
    summary_text: str
    bullets: list[RewrittenBullet] = Field(default_factory=list)


class TokenUsage(StrictModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LlmAdapterResponse(StrictModel):
    provider: ProviderName
    model: str
    proposal: SurgicalLlmProposal
    token_usage: TokenUsage


class LLMPort(Protocol):
    provider_name: ProviderName
    model_name: str

    async def adapt_once(self, sanitized: SanitizedSurgicalInput) -> LlmAdapterResponse:
        ...


RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "summary_text": {"type": "STRING"},
        "bullets": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "bullet_id": {"type": "STRING"},
                    "text": {"type": "STRING"},
                },
                "required": ["bullet_id", "text"],
            },
        },
    },
    "required": ["summary_text", "bullets"],
}


def _build_surgical_prompts(sanitized: SanitizedSurgicalInput) -> tuple[str, str]:
    lang_note = (
        "Write strictly in natural, confident English (en-US)."
        if sanitized.locale == "en-US"
        else "Escreva estritamente em Português do Brasil (pt-BR) natural, direto e confiante."
    )
    system_prompt = (
        "You are a surgical ATS resume copy-editor. Rewrite ONLY the provided summary_text and "
        "bullets to naturally incorporate the confirmed_job_keywords where accurate.\n"
        "STRICT RULES:\n"
        "1. NEVER invent tools, certifications, skills, metrics, numbers, or percentages.\n"
        "2. NEVER add self-deprecating qualifiers ('básico', 'noções', 'nível inicial', 'basic level', 'beginner').\n"
        "3. Preserve every placeholder token like [[INST_1]] or [[EMP_1]] verbatim.\n"
        "4. Keep every bullet_id unchanged and return valid JSON matching `{summary_text, bullets:[{bullet_id, text}]}`.\n"
        f"5. {lang_note}"
    )
    user_payload = json.dumps(sanitized.model_dump(mode="json"), ensure_ascii=False)
    return system_prompt, user_payload


def _make_http_client(
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
    if transport is not None:
        return httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False)
    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.AsyncClient(verify=ssl_ctx, timeout=timeout, follow_redirects=False)


def _parse_proposal_json(raw_text: str, provider: str) -> SurgicalLlmProposal:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = [ln for ln in cleaned.splitlines() if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        parsed = json.loads(cleaned)
        return SurgicalLlmProposal.model_validate(parsed)
    except Exception as exc:
        raise LlmAdapterError(f"{provider} returned malformed structured JSON output.") from exc


class GeminiAdapter:
    provider_name: ProviderName = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise LlmAdapterError("GEMINI_API_KEY is not configured.")
        self._api_key = api_key.strip()
        self.model_name = model
        self._timeout = timeout_seconds
        self._transport = transport

    async def adapt_once(self, sanitized: SanitizedSurgicalInput) -> LlmAdapterResponse:
        sys_prompt, user_json = _build_surgical_prompts(sanitized)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": sys_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_json}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_JSON_SCHEMA,
            },
        }
        async with _make_http_client(self._timeout, self._transport) as client:
            try:
                res = await client.post(
                    url,
                    json=body,
                    headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise LlmAdapterError(f"Gemini request failed: {type(exc).__name__}") from exc

        if res.status_code != 200:
            raise LlmAdapterError(f"Gemini API returned HTTP {res.status_code}.")

        data = res.json()
        try:
            text_out = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmAdapterError("Gemini response missing candidate text content.") from exc

        usage_meta = data.get("usageMetadata") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_meta.get("promptTokenCount") or 0),
            completion_tokens=int(usage_meta.get("candidatesTokenCount") or 0),
            total_tokens=int(usage_meta.get("totalTokenCount") or 0),
        )
        proposal = _parse_proposal_json(text_out, "Gemini")
        return LlmAdapterResponse(
            provider="gemini",
            model=self.model_name,
            proposal=proposal,
            token_usage=usage,
        )


class GroqAdapter:
    provider_name: ProviderName = "groq"

    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise LlmAdapterError("GROQ_API_KEY is not configured.")
        self._api_key = api_key.strip()
        self.model_name = model
        self._timeout = timeout_seconds
        self._transport = transport

    async def adapt_once(self, sanitized: SanitizedSurgicalInput) -> LlmAdapterResponse:
        sys_prompt, user_json = _build_surgical_prompts(sanitized)
        url = "https://api.groq.com/openai/v1/chat/completions"
        body = {
            "model": self.model_name,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_json},
            ],
        }
        async with _make_http_client(self._timeout, self._transport) as client:
            try:
                res = await client.post(
                    url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                raise LlmAdapterError(f"Groq request failed: {type(exc).__name__}") from exc

        if res.status_code != 200:
            raise LlmAdapterError(f"Groq API returned HTTP {res.status_code}.")

        data = res.json()
        try:
            text_out = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmAdapterError("Groq response missing choice message content.") from exc

        raw_usage = data.get("usage") or {}
        usage = TokenUsage(
            prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
            completion_tokens=int(raw_usage.get("completion_tokens") or 0),
            total_tokens=int(raw_usage.get("total_tokens") or 0),
        )
        proposal = _parse_proposal_json(text_out, "Groq")
        return LlmAdapterResponse(
            provider="groq",
            model=self.model_name,
            proposal=proposal,
            token_usage=usage,
        )


class OllamaAdapter:
    provider_name: ProviderName = "ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "llama3.2",
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self.model_name = model
        self._timeout = timeout_seconds
        self._transport = transport

    async def adapt_once(self, sanitized: SanitizedSurgicalInput) -> LlmAdapterResponse:
        sys_prompt, user_json = _build_surgical_prompts(sanitized)
        url = f"{self._base_url}/api/chat"
        schema = {
            "type": "object",
            "properties": {
                "summary_text": {"type": "string"},
                "bullets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "bullet_id": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["bullet_id", "text"],
                    },
                },
            },
            "required": ["summary_text", "bullets"],
        }
        body = {
            "model": self.model_name,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.2},
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_json},
            ],
        }
        async with _make_http_client(self._timeout, self._transport) as client:
            try:
                res = await client.post(url, json=body)
            except httpx.HTTPError as exc:
                raise LlmAdapterError(f"Ollama local request failed: {type(exc).__name__}") from exc

        if res.status_code != 200:
            raise LlmAdapterError(f"Ollama API returned HTTP {res.status_code}.")

        data = res.json()
        try:
            text_out = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LlmAdapterError("Ollama response missing message content.") from exc

        p_tok = int(data.get("prompt_eval_count") or 0)
        c_tok = int(data.get("eval_count") or 0)
        usage = TokenUsage(
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=p_tok + c_tok,
        )
        proposal = _parse_proposal_json(text_out, "Ollama")
        return LlmAdapterResponse(
            provider="ollama",
            model=self.model_name,
            proposal=proposal,
            token_usage=usage,
        )


def build_llm_adapter(
    settings: AppSettings,
    provider_override: ProviderName | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LLMPort:
    chosen = provider_override or settings.llm_provider
    if chosen == "none":
        raise LlmAdapterError(
            "Surgical AI mode is disabled (LLM_PROVIDER='none'). Choose 'gemini', 'groq', or 'ollama'."
        )
    if chosen == "gemini":
        return GeminiAdapter(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            timeout_seconds=settings.llm_timeout_seconds,
            transport=transport,
        )
    if chosen == "groq":
        return GroqAdapter(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            timeout_seconds=settings.llm_timeout_seconds,
            transport=transport,
        )
    if chosen == "ollama":
        return OllamaAdapter(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout_seconds=settings.llm_timeout_seconds,
            transport=transport,
        )
    raise LlmAdapterError(f"Unsupported LLM provider '{chosen}'.")
