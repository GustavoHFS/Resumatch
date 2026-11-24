"""Exclusive Google Gemini async adapter (`GeminiAdapter`) via lightweight `httpx`.

- Zero heavy vendor SDKs; uses OS truststore TLS, header-only secret hygiene (`x-goog-api-key`),
   and bounded retry/fallback across Flash Lite, Flash and compatible discovered models.
- Supports real-time zero-cost key validation (`validate_connection`), block-aware Base Resume
  extraction (`extract_profile_from_text`), and tailored resume generation (`generate_tailored_resume`).
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from typing import Any, Callable, Literal, Protocol

import httpx
import truststore
from pydantic import Field

from app.adapters.llm.pii_stripper import SanitizedResumeGenInput
from app.core.config import AppSettings, DEFAULT_GEMINI_MODELS
from app.models.profile_schema import StrictModel

ProviderName = Literal["gemini"]


class LlmAdapterError(RuntimeError):
    """Safe error raised on Gemini transport, quota, auth, or schema failures (zero secret leakage)."""


class TokenUsage(StrictModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LlmJsonCallResult(StrictModel):
    provider: ProviderName = "gemini"
    model: str
    data: dict[str, Any] = Field(default_factory=dict)
    token_usage: TokenUsage


class LLMPort(Protocol):
    provider_name: ProviderName
    model_name: str

    async def validate_connection(self) -> str:
        ...

    async def generate_tailored_resume(
        self, sanitized: SanitizedResumeGenInput
    ) -> LlmJsonCallResult:
        ...

    async def extract_profile_from_text(self, masked_text: str) -> LlmJsonCallResult:
        ...


RESUME_GENERATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_role": {"type": "string"},
        "section_order": {
            "type": "array",
            "items": {"type": "string"},
        },
        "summary": {"type": "string", "maxLength": 420},
        "experiences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "role": {"type": "string"},
                    "company": {"type": "string"},
                    "period": {"type": "string"},
                    "description": {"type": "string"},
                    "highlights": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "role", "company", "period", "highlights"],
            },
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "degree": {"type": "string"},
                    "institution": {"type": "string"},
                    "period": {"type": "string"},
                    "details": {"type": "string", "maxLength": 0},
                },
                "required": ["id", "degree", "institution", "period", "details"],
            },
        },
        "courses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "institution": {"type": "string"},
                    "workload": {"type": "string"},
                    "completion_date": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["id", "title", "institution", "workload", "completion_date"],
            },
        },
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "description": {"type": "string"},
                    "highlights": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "name", "description"],
            },
        },
        "skill_groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "label", "items"],
            },
        },
        "languages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "language": {"type": "string"},
                    "proficiency": {"type": "string"},
                },
                "required": ["id", "language", "proficiency"],
            },
        },
    },
    "required": [
        "target_role",
        "section_order",
        "summary",
        "experiences",
        "education",
        "courses",
        "projects",
        "skill_groups",
        "languages",
    ],
}

PROFILE_EXTRACT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string"},
        "city": {"type": "string"},
        "state": {"type": "string"},
        "summary": {"type": "string"},
        "experiences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "company": {"type": "string"},
                    "period": {"type": "string"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["role", "company", "period", "description"],
            },
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "degree": {"type": "string"},
                    "institution": {"type": "string"},
                    "period": {"type": "string"},
                    "details": {"type": "string"},
                },
                "required": ["degree", "institution", "period", "details"],
            },
        },
        "courses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "institution": {"type": "string"},
                    "workload": {"type": "string"},
                    "completion_date": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "institution", "workload", "completion_date", "description"],
            },
        },
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
            },
        },
        "skills": {"type": "array", "items": {"type": "string"}},
        "languages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "language": {"type": "string"},
                    "proficiency": {"type": "string"},
                },
                "required": ["language", "proficiency"],
            },
        },
    },
    "required": [
        "full_name",
        "summary",
        "experiences",
        "education",
        "courses",
        "projects",
        "skills",
        "languages",
    ],
}


def build_resume_generation_prompts(sanitized: SanitizedResumeGenInput) -> tuple[str, str]:
    exp_count = len(sanitized.experiences)
    if sanitized.max_pages == 1:
        if exp_count <= 2:
            density_rule = (
                "DENSIDADE INTELIGENTE (PERFIL JÚNIOR / TRANSIÇÃO COM 1–2 EXPERIÊNCIAS): "
                "NÃO enxugue excessivamente deixando meia folha A4 vazia! Extraia de 5 a 7 `highlights` ricos e concretos por experiência, "
                "preservando TODAS as frentes transferíveis do candidato (ex.: atendimento/suporte, administração de acessos, infraestrutura/redes, automação, planilhas/relatórios, dados ou código) "
                "conectadas à vaga, além de incluir todas as formações, cursos relevantes e de 3 a 5 grupos bem preenchidos em `skill_groups`."
            )
        else:
            density_rule = (
                "ORÇAMENTO DE 1 PÁGINA A4: selecione de 3 a 5 `highlights` de maior impacto por experiência e priorize os cursos, projetos e competências mais aderentes."
            )
    else:
        density_rule = (
            "ATÉ 2 PÁGINAS A4 PERMITIDAS: apresente histórico detalhado com `highlights` completos, projetos, cursos e certificações relevantes."
        )

    system_prompt = (
        "Você é um Estrategista Sênior de Currículos e Sistemas ATS no Brasil (Gupy, Inhire, Empregare, Greenhouse, Workday).\n"
        "Com base no Currículo Base completo do candidato e nos dados da vaga-alvo, monte um rascunho de currículo personalizado em JSON.\n"
        "REGRAS ESTRITAS DE FIDELIDADE, ESPELHAMENTO ATS E ESTILO:\n"
        "1. ESTRUTURA E ORDENAÇÃO: defina `target_role` alinhado ao cargo da vaga (fiel ao nível real do candidato) e ordene `section_order` "
        "(valores válidos: 'summary', 'experience', 'education', 'projects', 'courses', 'skills', 'languages') colocando os pontos mais fortes primeiro. "
        "Agrupe as competências reais do candidato em 3 a 5 `skill_groups` claros (`{id, label, items}`).\n"
        "2. ESPELHAMENTO TERMINOLÓGICO EXATO PARA ATS (SEM INVENÇÃO): quando a vaga pedir um termo equivalente a algo que JÁ CONSTA no Currículo Base "
        "(ex.: perfil cita 'chamados / Help Desk' e a vaga pede 'Service Desk / atendimento de tickets'; perfil cita 'planilhas de orçamento' e a vaga pede 'controle em Excel'), "
        "use a terminologia exata da vaga nos `highlights`, no `summary` e em `skill_groups`. JAMAIS invente ferramentas, linguagens, empresas, cargos, certificações ou números inexistentes.\n"
        "3. TRANSFORMAÇÃO DE EXPERIÊNCIAS E DATAS LIMPAS: converta o texto livre (`description`) de cada experiência em `highlights` objetivos iniciados por substantivo ou verbo de ação concreto "
        "(deixando `description` como `\"\"` quando preencher `highlights`). Padronize `period` em formato limpo PT-BR (ex.: `Mar/2024 – Set/2024` ou `Jul/2026 – Dez/2028`), sem colar regime de contratação se poluir a data.\n"
        "4. PROIBIÇÃO DE CLICHÊS DE IA: é ESTRITAMENTE PROIBIDO usar expressões vazias ou robóticas como 'altamente motivado', 'apaixonado por tecnologia', "
        "'proativo e dinâmico', 'sinergia', 'focado em resultados', 'mentalidade ágil', 'busca incessante' ou elogios genéricos sem fato concreto.\n"
        "5. PRIVACIDADE E PLACEHOLDERS: preserve todos os tokens `[[EMP_1]]`, `[[INST_1]]`, `[[URL_1]]` EXATAMENTE como recebidos "
        "(em `company`, repasse `company_token` literalmente; em `institution`, repasse `institution_token` literalmente).\n"
        f"6. {density_rule}\n"
        "7. RESUMO PROFISSIONAL: um único parágrafo curto, de 3 a 4 linhas reais em A4, "
        "no máximo 420 caracteres. Condense formação principal, prática relevante e valor específico para a vaga. "
        "Não copie o resumo longo da base nem repita listas de ferramentas já presentes nas competências.\n"
        "8. FORMAÇÃO ACADÊMICA: mantenha todos os itens e a ordem recebida, sem rebaixar ou omitir "
        "o ensino superior em andamento em função da vaga. Somente Curso/Grau, Período/Status e Instituição. "
        "Nunca inclua descrições, ementas ou notas entre parênteses; details deve ser vazio.\n"
        "9. Escreva 100% em Português do Brasil (pt-BR) natural, direto e técnico.\n"
        "Retorne APENAS JSON válido seguindo o schema solicitado."
    )
    user_json = json.dumps(sanitized.model_dump(mode="json"), ensure_ascii=False)
    return system_prompt, user_json


def build_profile_extraction_prompts(masked_text: str) -> tuple[str, str]:
    system_prompt = (
        "Você é um extrator especialista de currículos brasileiros. Analise o documento por BLOCOS VISUAIS E SEMÂNTICOS de linhas próximas, nunca preenchendo campos às cegas.\n"
        "REGRAS OBRIGATÓRIAS DE AGRUPAMENTO:\n"
        "1. FORMAÇÃO (`education`): identifique cada bloco completo vinculando Grau + Instituição + Período no MESMO registro.\n"
        "   - Reconheça graus como `Graduação em...`, `Bacharelado`, `Tecnólogo`, `Licenciatura`, `Curso Técnico em...`, `Especialização`, incluindo status como `- Em andamento`.\n"
        "   - Reconheça siglas e instituições brasileiras como `UNICAP`, `UNINTER`, `UNINASSAU`, `UFPE`, `UFRPE`, `IFPE`, `ETEMAC`, `SENAI`, `SENAC`, `FIAP`, `Universidade`, `Centro Universitário`, `Faculdade`, `Escola Técnica`, preservando complementos como `(via Embarque Digital)` em `details` ou junto à instituição.\n"
        "   - Capture períodos completos como `Início: Jul/2026 · Previsão de conclusão: Dez/2028`, `Concluído em Dez/2025` ou `MM/AAAA` em `period`.\n"
        "2. EXPERIÊNCIAS (`experiences`): agrupe Cargo (`role`), Empresa (`company`), Vínculo/Período (`period`) e consolide integralmente todas as atividades daquela função num único texto corrido coeso em `description`.\n"
        "3. CURSOS E CERTIFICAÇÕES (`courses`): separe corretamente `title` (Título), `institution` (Instituição), `workload` (Carga Horária em horas, ex: `40 horas`), `completion_date` (Conclusão, ex: `2026` ou `Out/2026`) e `description` (descrição prática ou conteúdo).\n"
        "4. PROJETOS (`projects`): extraia apenas `name`, `url` (se houver) e `description`.\n"
        "5. Preserve todos os placeholders `[[EMAIL_1]]`, `[[PHONE_1]]` ou `[[URL_1]]` literalmente e não invente fatos ausentes no texto."
    )
    return system_prompt, masked_text[:60000]


def _make_http_client(
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
    if transport is not None:
        return httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False)
    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.AsyncClient(verify=ssl_ctx, timeout=timeout, follow_redirects=False)


def _parse_json_dict(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = [ln for ln in cleaned.splitlines() if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("Expected JSON object")
        return parsed
    except Exception as exc:
        raise LlmAdapterError("A resposta da IA do Google veio em formato inesperado. Tente novamente.") from exc


class GeminiAdapter:
    provider_name: ProviderName = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash-lite",
        fallback_model: str = "gemini-2.5-flash",
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        on_model_selected: Callable[[str], None] | None = None,
        total_timeout_seconds: float = 55.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if not api_key.strip():
            raise LlmAdapterError(
                "Conecte sua chave gratuita do Google para gerar currículos personalizados."
            )
        self._api_key = api_key.strip()
        self.model_name = model
        self._fallback_model = fallback_model
        self._timeout = timeout_seconds
        self._transport = transport
        self._on_model_selected = on_model_selected
        self._total_timeout = min(55.0, total_timeout_seconds)
        self._sleep = sleep
        self._without_thinking: set[str] = set()

    @staticmethod
    def _valid_model(model: str) -> bool:
        return bool(re.fullmatch(r"gemini-[A-Za-z0-9._-]+", model)) and not any(
            variant in model.lower() for variant in
            ("embedding", "imagen", "veo", "tts", "robotics", "computer-use")
        )

    def _models_to_try(self) -> list[str]:
        return [model for model in dict.fromkeys([
            self.model_name, *DEFAULT_GEMINI_MODELS, self._fallback_model,
        ]) if self._valid_model(model)]

    async def _discover_models(self, client: httpx.AsyncClient) -> list[str]:
        """Read the model catalog without exposing credentials in URLs or logs."""
        try:
            response = await client.get("https://generativelanguage.googleapis.com/v1beta/models",
                                        headers={"x-goog-api-key": self._api_key})
            if response.status_code != 200:
                return []
            models = response.json().get("models", [])
            return list(dict.fromkeys(
                item["name"].removeprefix("models/") for item in models
                if isinstance(item, dict) and isinstance(item.get("name"), str)
                and self._valid_model(item["name"].removeprefix("models/"))
                and "generateContent" in (item.get("supportedGenerationMethods") or [])
            ))[:8]
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            return []

    def _remember_model(self, model: str) -> None:
        self.model_name = model
        if self._on_model_selected is not None:
            self._on_model_selected(model)

    def _google_error(self, response: httpx.Response) -> LlmAdapterError:
        try:
            payload = response.json()
            message = payload.get("error", {}).get("message", "")
        except (ValueError, AttributeError, TypeError):
            message = ""
        if not isinstance(message, str):
            message = ""
        safe = message.replace(self._api_key, "[CHAVE OCULTA]").strip()[:500]
        suffix = f" Detalhes do Google: {safe}" if safe else ""
        return LlmAdapterError(f"Não foi possível acessar o Google (HTTP {response.status_code}).{suffix}")

    async def validate_connection(self) -> str:
        """Validates the Google AI Studio key in real time with zero token cost."""
        models_to_try = self._models_to_try()
        async with _make_http_client(min(8.0, self._timeout), self._transport) as client:
            for candidate_model in models_to_try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{candidate_model}"
                try:
                    res = await client.get(url, headers={"x-goog-api-key": self._api_key})
                except httpx.HTTPError as exc:
                    raise LlmAdapterError(
                        f"Não foi possível conectar ao Google no momento ({type(exc).__name__})."
                    ) from exc

                if res.status_code == 200:
                    self._remember_model(candidate_model)
                    return candidate_model
                if res.status_code in {400, 401, 403}:
                    raise self._google_error(res)

            discovered = await self._discover_models(client)
            if discovered:
                self._remember_model(discovered[0])
                return discovered[0]

        raise self._google_error(res)

    async def _call_structured(
        self, sys_prompt: str, user_text: str, schema: dict[str, Any]
    ) -> LlmJsonCallResult:
        models_to_try = self._models_to_try()
        body = {
            "systemInstruction": {"parts": [{"text": sys_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
        try:
            async with asyncio.timeout(self._total_timeout):
                res, used_model = await self._generate_with_fallback(body, models_to_try)
        except TimeoutError as exc:
            raise LlmAdapterError(
                "O Google não respondeu dentro do prazo, mesmo com modelos alternativos. "
                "Tente novamente em instantes; nenhum currículo foi salvo."
            ) from exc

        data = res.json()
        try:
            candidate_obj = data["candidates"][0]
            if candidate_obj.get("finishReason") == "MAX_TOKENS":
                raise LlmAdapterError(
                    "O rascunho excedeu o tamanho máximo de resposta da IA. Tente resumir a descrição ou usar 1 página."
                )
            text_out = candidate_obj["content"]["parts"][0]["text"]
        except LlmAdapterError:
            raise
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmAdapterError("A resposta do Google veio vazia.") from exc

        usage_meta = data.get("usageMetadata") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_meta.get("promptTokenCount") or 0),
            completion_tokens=int(usage_meta.get("candidatesTokenCount") or 0),
            total_tokens=int(usage_meta.get("totalTokenCount") or 0),
        )
        self._remember_model(used_model)
        return LlmJsonCallResult(
            provider="gemini",
            model=used_model,
            data=_parse_json_dict(text_out),
            token_usage=usage,
        )

    async def _generate_with_fallback(
        self, body: dict[str, Any], models: list[str],
    ) -> tuple[httpx.Response, str]:
        deadline = time.monotonic() + self._total_timeout
        attempts: dict[str, int] = {}
        retry_models: list[str] = []
        discovered = False
        failures = 0
        last_error = LlmAdapterError("Nenhum modelo de texto do Google está disponível no momento.")
        queue = list(models)
        async with _make_http_client(min(8.0, self._timeout), self._transport) as client:
            index = 0
            while index < len(queue) or not discovered:
                if index == len(queue):
                    discovered = True
                    dynamic = await self._discover_models(client)
                    queue.extend(model for model in dynamic if model not in attempts)
                    queue.extend(retry_models)
                    if index == len(queue):
                        break
                model = queue[index]
                index += 1
                attempts[model] = attempts.get(model, 0) + 1
                config = dict(body["generationConfig"])
                if model.startswith("gemini-2.5-") and model not in self._without_thinking:
                    config["thinkingConfig"] = {"thinkingBudget": 0}
                request_body = {**body, "generationConfig": config}
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                while True:
                    try:
                        response = await client.post(
                            url, json=request_body, headers={"x-goog-api-key": self._api_key},
                            timeout=httpx.Timeout(max(.01, min(8.0, self._timeout, deadline - time.monotonic()))),
                        )
                    except httpx.RequestError as exc:
                        last_error = LlmAdapterError(
                            f"Falha de rede ou tempo limite ao comunicar com o Google ({type(exc).__name__})."
                        )
                        transient = True
                        break
                    if response.status_code == 200:
                        return response, model
                    # Disable only an explicitly rejected optional field, never retry bad credentials/payloads.
                    error = self._google_error(response)
                    if response.status_code == 400 and "thinkingConfig" in config and any(
                        field in str(error).lower() for field in ("thinkingconfig", "thinking_config", "thinkingbudget", "thinking_budget")
                    ):
                        self._without_thinking.add(model)
                        config.pop("thinkingConfig")
                        continue
                    transient = response.status_code == 429 or 500 <= response.status_code <= 599
                    if not transient and response.status_code != 404:
                        raise error
                    last_error = error
                    break
                if transient:
                    if attempts[model] < 2:
                        if discovered:
                            queue.append(model)
                        else:
                            retry_models.append(model)
                    delay = min(1.5 * 2 ** min(failures, 1), max(0, deadline - time.monotonic()))
                    failures += 1
                    await self._sleep(delay)
        raise LlmAdapterError(
            f"Os modelos alternativos do Google foram tentados sem sucesso. {last_error}"
        )

    async def generate_tailored_resume(
        self, sanitized: SanitizedResumeGenInput
    ) -> LlmJsonCallResult:
        sys_prompt, user_json = build_resume_generation_prompts(sanitized)
        return await self._call_structured(sys_prompt, user_json, RESUME_GENERATION_JSON_SCHEMA)

    async def extract_profile_from_text(self, masked_text: str) -> LlmJsonCallResult:
        sys_prompt, user_text = build_profile_extraction_prompts(masked_text)
        return await self._call_structured(sys_prompt, user_text, PROFILE_EXTRACT_JSON_SCHEMA)


def build_llm_adapter(
    settings: AppSettings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GeminiAdapter:
    return GeminiAdapter(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        fallback_model=settings.gemini_fallback_model,
        timeout_seconds=settings.llm_timeout_seconds,
        transport=transport,
        on_model_selected=lambda model: setattr(settings, "gemini_model", model),
        total_timeout_seconds=settings.llm_total_timeout_seconds,
    )
