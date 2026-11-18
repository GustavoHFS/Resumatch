"""Human-readable Portuguese labels for internal triage, automation, and Kanban status codes.

Ensures zero `snake_case` enum tokens are surfaced to end users in API responses or UI views.
"""

from __future__ import annotations

from typing import Any

CLASSIFICATION_PT: dict[str, str] = {
    "aprovada": "Aprovada",
    "aprovada_com_preparacao": "Aprovada com estudo rápido",
    "elegivel_para_avaliacao": "Elegível para avaliação",
    "pendente": "Informação pendente",
    "descartada": "Fora dos critérios",
    "fora_foco": "Senioridade acima do foco",
    "fora_estrategia": "Fora da estratégia",
}

COMPATIBILITY_PT: dict[str, str] = {
    "compativel": "Compatível",
    "compativel_com_preparacao": "Compatível com estudo rápido",
    "insuficiente_para_avaliar": "Descrição insuficiente",
    "incompativel_por_impedimento_concreto": "Incompatível",
}

OPERATIONAL_PT: dict[str, str] = {
    "pronta_para_preparar_candidatura": "Pronta para preparar currículo",
    "necessita_confirmar_informacao_importante": "Detalhes a confirmar no anúncio",
    "necessita_intervencao_usuario": "Envio manual em portal",
    "encerrada_ou_indisponivel": "Encerrada ou indisponível",
}

AUTOMATION_CAPABILITY_PT: dict[str, str] = {
    "automacao_possivel": "Canal direto disponível",
    "candidatura_assistida": "Preparação assistida",
    "apenas_descoberta": "Apenas descoberta",
    "necessita_intervencao_manual": "Portal com login próprio",
    "desconhecida": "Canal a verificar",
}

STAGE_PT: dict[str, str] = {
    "descoberta": "Descoberta",
    "candidatura_a_preparar": "A preparar",
    "curriculo_preparado": "Currículo pronto",
    "aguardando_envio_manual": "Pronta para enviar",
    "enviada_manualmente": "Enviada",
    "encerrada_ou_descartada": "Arquivada",
}

WORKPLACE_PT: dict[str, str] = {
    "remoto": "Remoto",
    "hibrido": "Híbrido",
    "presencial": "Presencial",
}

CAREER_TIER_PT: dict[str, str] = {
    "entrada": "Início de carreira / Júnior",
    "avaliar": "Pleno / A avaliar",
    "fora_foco": "Sênior / Especialista",
}

SERVICE_TIER_PT: dict[int, str] = {
    1: "Canal direto",
    2: "Preparação local",
    3: "Portal externo",
}


def humanize_code(mapping: dict[Any, str], code: Any, fallback: str = "Não informado") -> str:
    if code is None or code == "":
        return fallback
    if code in mapping:
        return mapping[code]
    text = str(code).replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else fallback


def build_job_human_labels(
    *,
    classification: str | None = None,
    compatibility: str | None = None,
    operational_status: str | None = None,
    automation_capability: str | None = None,
    stage: str | None = None,
    workplace_mode: str | None = None,
    career_tier: str | None = None,
    service_tier: int | None = None,
) -> dict[str, str]:
    return {
        "classification_label": humanize_code(CLASSIFICATION_PT, classification),
        "compatibility_label": humanize_code(COMPATIBILITY_PT, compatibility),
        "operational_label": humanize_code(OPERATIONAL_PT, operational_status),
        "automation_label": humanize_code(AUTOMATION_CAPABILITY_PT, automation_capability),
        "stage_label": humanize_code(STAGE_PT, stage, "Descoberta"),
        "workplace_label": humanize_code(WORKPLACE_PT, workplace_mode, "Modalidade não informada"),
        "career_tier_label": humanize_code(CAREER_TIER_PT, career_tier, "A avaliar"),
        "service_tier_label": humanize_code(SERVICE_TIER_PT, service_tier, "Preparação local"),
    }
