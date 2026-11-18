"""Unified Deterministic Job Triage Engine (Zero AI).

Consolidates lexical requirement extraction, section-aware negation/desirable parsing,
hard-constraint filtering, operational omissions tracking, and 3-tier automation
capability classification — 100% driven by `SearchCriteria`, `BlockedCompaniesConfig`,
and `MasterProfile` (zero personal rules hardcoded in source code).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field

from app.domain.labels_pt import build_job_human_labels
from app.models.blocked_schema import BlockedCompaniesConfig
from app.models.criteria_schema import SearchCriteria
from app.models.profile_schema import MasterProfile, StrictModel

EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)

PLATAFORMAS_LOGIN: dict[str, tuple[str, str]] = {
    "indeed": ("indeed.", "Indeed"),
    "infojobs": ("infojobs.", "InfoJobs"),
    "linkedin": ("linkedin.", "LinkedIn"),
    "gupy": ("gupy.io", "Gupy"),
    "empregare": ("empregare.com", "Empregare"),
    "catho": ("catho.", "Catho"),
    "vagas": ("vagas.com", "Vagas.com.br"),
    "solides": ("solides", "Sólides"),
    "trabalhabrasil": ("trabalhabrasil", "Trabalha Brasil"),
    "bne": ("bne.com.br", "BNE"),
}

BRAZIL_STATE_CODES = {
    "ac": "acre",
    "al": "alagoas",
    "am": "amazonas",
    "ap": "amapa",
    "ba": "bahia",
    "ce": "ceara",
    "df": "distrito federal",
    "es": "espirito santo",
    "go": "goias",
    "ma": "maranhao",
    "mg": "minas gerais",
    "ms": "mato grosso do sul",
    "mt": "mato grosso",
    "pa": "para",
    "pb": "paraiba",
    "pe": "pernambuco",
    "pi": "piaui",
    "pr": "parana",
    "rj": "rio de janeiro",
    "rn": "rio grande do norte",
    "ro": "rondonia",
    "rr": "roraima",
    "rs": "rio grande do sul",
    "sc": "santa catarina",
    "se": "sergipe",
    "sp": "sao paulo",
    "to": "tocantins",
}


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", (value or "").casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.replace("–", "-").replace("—", "-")


def contains_term(haystack: str, needle: str) -> bool:
    norm_needle = normalize_text(needle).strip()
    if not norm_needle:
        return False
    pattern = r"(?<!\w)" + re.escape(norm_needle) + r"(?!\w)"
    return re.search(pattern, normalize_text(haystack)) is not None


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"gh_src", "lever-source", "source", "ref"}
    ]
    netloc = parsed.netloc.lower().replace("boards.greenhouse.io", "job-boards.greenhouse.io")
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), urlencode(sorted(query)), "")
    )


def compute_job_fingerprint(
    company: str | None,
    title: str | None,
    location: str | None,
    description: str | None,
) -> str | None:
    fields = [company, title, location, description]
    if not all(fields):
        return None
    normalized = "|".join(
        re.sub(r"\s+", " ", normalize_text(str(val))).strip() for val in fields
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_company_name(value: str | None) -> str:
    base = normalize_text(value or "").strip().rstrip(".")
    return re.sub(r"\s+(?:ltda|s/?a|eireli|me|epp)\.?$", "", base).strip()


CONFIDENTIAL_COMPANY_NAMES = {
    "",
    "confidencial",
    "empresa confidencial",
    "empresa",
    "nao informada",
    "nao informado",
    "sigiloso",
    "oculto",
    "a combinar",
    "diversas empresas",
}


def _slugify_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_text(value)).strip("-")


def compute_cross_portal_signature(
    company: str | None,
    title: str | None,
    location: str | None = None,
    workplace_mode: str | None = None,
) -> str | None:
    """Computes `xportal:<slug(company)>__<slug(clean_title)>__<slug(city_or_remoto)>`
    to unify identical jobs published simultaneously across multiple job portals."""
    comp_norm = _normalize_company_name(company)
    if comp_norm in CONFIDENTIAL_COMPANY_NAMES or len(comp_norm) < 2:
        return None
    comp_slug = _slugify_token(comp_norm)
    if not comp_slug:
        return None

    t = normalize_text(title or "").strip()
    if len(t) < 3:
        return None
    # Strip portal prefixes/suffixes, bracketed tags, and trailing location suffixes
    t = re.sub(r"^\[[^\]]+\]\s*", "", t)
    t = re.sub(r"\((?:remoto|hibrido|presencial|clt|pj|vaga\s+afirmativa[^)]*)\)", " ", t)
    t = re.sub(r"^vaga\s+(?:de|para)\s+", "", t)
    t = re.sub(r"\s+[-/·|]\s+(?:recife|olinda|jaboatao|paulista|camaragibe|pernambuco|pe|brasil|remoto|home\s*office).*$", "", t)
    t = re.sub(r"\bpessoa\s+desenvolvedora\b", "desenvolvedor", t)
    t = re.sub(r"\bdesenvolvedora\b", "desenvolvedor", t)
    t = re.sub(r"\banalista\s+de\b", "analista", t)
    t = re.sub(r"\bjr\.?\b", "junior", t)
    t = re.sub(r"\bpl\.?\b", "pleno", t)
    t = re.sub(r"\bsr\.?\b", "senior", t)
    title_slug = _slugify_token(t)
    if not title_slug:
        return None

    loc_norm = normalize_text(location or "").strip()
    wp_norm = normalize_text(workplace_mode or "").strip()
    if (
        wp_norm == "remoto"
        or "remoto" in loc_norm
        or "home office" in loc_norm
        or "todo o brasil" in loc_norm
        or "remote" in loc_norm
        or "remoto" in normalize_text(title or "")
    ):
        loc_slug = "remoto"
    elif loc_norm:
        primary_city = re.split(r"[,;/\-·|]", loc_norm)[0].strip()
        primary_city = re.sub(r"\b(?:br|brasil|brazil|estado\s+de\s+\w+)\b", "", primary_city).strip()
        loc_slug = _slugify_token(primary_city) or "geral"
    else:
        loc_slug = "geral"

    return f"xportal:{comp_slug}__{title_slug}__{loc_slug}"


class RawJobInput(StrictModel):
    source: str = "manual:import"
    source_id: str | None = None
    title: str = Field(min_length=2, max_length=300)
    company: str | None = Field(default=None, max_length=200)
    url: str = Field(min_length=8, max_length=1000)
    description: str = Field(default="", max_length=100000)
    location: str | None = Field(default=None, max_length=200)
    workplace_mode: Literal["presencial", "hibrido", "remoto"] | None = None
    contract_hint: str | None = None
    country: str | None = None
    published_at: str | None = None
    updated_at_source: str | None = None
    official_deadline: str | None = None
    opportunity_type: Literal["vaga", "recrutador", "banco_talentos", "prospeccao"] = "vaga"
    validity_status: str = "aparentemente_aberta"
    pdf_confirmed: bool = False
    requires_registration: bool = False
    short_form: bool = False
    process_steps: list[str] = Field(default_factory=list)
    massive_program: bool = False
    company_size: str | None = None
    incomplete_summary: bool = False
    access_blocked: bool = False
    requires_login: bool = False
    public_channel: str | None = None
    source_method_hint: Literal["A", "B", "C", "D"] | None = None
    source_effort_hint: Literal["baixo", "medio", "alto"] | None = None
    company_relation_uncertain: bool = False


class MatchedRequirement(StrictModel):
    skill_name: str
    matched_keywords: list[str]
    requirement_kind: Literal["mandatory", "desirable", "responsibility", "optional", "unspecified"]
    excerpt: str


class ShortPrepPlanItem(StrictModel):
    term: str
    study_plan: str
    acquired: bool = False


class UnmappedRequirement(StrictModel):
    text: str
    requirement_kind: str


class TriageResult(StrictModel):
    # Human-readable Portuguese labels (zero snake_case)
    labels: dict[str, str] = Field(default_factory=dict)

    # Axis 1: Professional Compatibility & Classification
    classification: Literal[
        "aprovada",
        "aprovada_com_preparacao",
        "elegivel_para_avaliacao",
        "pendente",
        "descartada",
        "fora_foco",
        "fora_estrategia",
    ]
    professional_compatibility: Literal[
        "compativel",
        "compativel_com_preparacao",
        "insuficiente_para_avaliar",
        "incompativel_por_impedimento_concreto",
    ]
    local_prep_allowed: bool
    area_id: str | None
    candidate_areas: list[str]
    area_scores: dict[str, int]
    career_tier: Literal["entrada", "avaliar", "fora_foco"]
    relevance_score: int
    practical_priority: int

    # Incompatibilities, Omissions, & Technical Notes
    hard_incompatibilities: list[str]
    strategic_exclusions: list[str]
    blocking_pendencies: list[str]
    operational_omissions: list[str]
    technical_review_notes: list[str]

    # Axis 2: Operational Status & Extracted Metadata
    operational_status: Literal[
        "pronta_para_preparar_candidatura",
        "necessita_confirmar_informacao_importante",
        "necessita_intervencao_usuario",
        "encerrada_ou_indisponivel",
    ]
    inferred_workplace_mode: str | None
    inferred_contract: str | None
    matched_city_slug: str | None
    geography_state: Literal["allowed", "outside", "pending"]

    # Skills & Short Prep Breakdown
    matched_skills: list[str]
    preparable_gaps: list[str] = Field(default_factory=list)
    requirements: list[MatchedRequirement]
    short_prep_plans: list[ShortPrepPlanItem]
    unmapped_requirements: list[UnmappedRequirement]

    # Axis 3: Automation Capability & Application Method
    application_method: Literal["A", "B", "C", "D", "desconhecido"]
    application_effort: Literal["baixo", "medio", "alto", "desconhecido"]
    pdf_status: Literal["confirmado", "verificar", "desconhecido"]
    service_tier: Literal[1, 2, 3]
    automation_capability: Literal[
        "automacao_possivel",
        "candidatura_assistida",
        "apenas_descoberta",
        "necessita_intervencao_manual",
        "desconhecida",
    ]
    platform_name: str | None
    submission_instructions: list[str]
    automation_reasons: list[str]
    initial_application_stage: Literal["descoberta", "encerrada_ou_descartada"]


class DeterministicTriageEngine:
    """Unified, stateless deterministic evaluator driven purely by configuration schemas."""

    def __init__(
        self,
        criteria: SearchCriteria,
        blocked: BlockedCompaniesConfig,
        profile: MasterProfile,
    ) -> None:
        self.criteria = criteria
        self.blocked = blocked
        self.profile = profile
        self._skill_terms: list[tuple[str, list[str], re.Pattern[str]]] = []
        self._norm_skill_tokens: set[str] = set()

        for raw_tag in profile.skills:
            base = re.sub(r"\([^)]*\)", "", raw_tag).strip()
            inside_parens = [
                part.strip()
                for grp in re.findall(r"\(([^)]+)\)", raw_tag)
                for part in re.split(r"[,/]", grp)
                if part.strip()
            ]
            split_conj = [
                p.strip()
                for p in re.split(r"\s+(?:e|and|/)\s+", base, flags=re.IGNORECASE)
                if len(p.strip()) >= 2
            ]
            variants = [
                v for v in dict.fromkeys([raw_tag.strip(), base, *inside_parens, *split_conj]) if v
            ]
            norm_variants = [normalize_text(v) for v in variants if normalize_text(v)]
            for nv in norm_variants:
                self._norm_skill_tokens.add(nv)
                self._norm_skill_tokens.add(nv.replace(" ", "_"))
            if norm_variants:
                pattern = re.compile(
                    r"(?<!\w)(?:" + "|".join(re.escape(nv) for nv in norm_variants) + r")(?!\w)"
                )
                self._skill_terms.append((raw_tag, variants, pattern))

    def evaluate_geography(self, location_text: str | None) -> tuple[Literal["allowed", "outside", "pending"], str | None]:
        raw_norm = normalize_text(location_text or "").strip()
        if not raw_norm:
            return "pending", None

        if ";" in raw_norm:
            parts = [self.evaluate_geography(p) for p in raw_norm.split(";") if p.strip()]
            for state, slug in parts:
                if state == "allowed":
                    return "allowed", slug

        allowed_state_codes = {c.state_code.lower() for c in self.criteria.geography.allowed_cities}
        allowed_state_names = {
            BRAZIL_STATE_CODES[code] for code in allowed_state_codes if code in BRAZIL_STATE_CODES
        }

        # Broader metropolitan region ("Região Metropolitana" / "Grande <City>" / "RMR") is NOT city confirmation
        cleaned = re.sub(
            r"regiao metropolitana(?:\s+d[eo]\s+[\w\s]+)?|\brmr\b",
            "regiao_metropolitana_ampla",
            raw_norm,
        )
        # Avoid matching street names like "Av. <CityName>" as city
        for city_rule in self.criteria.geography.allowed_cities:
            for alias in city_rule.aliases:
                cleaned = re.sub(
                    r"\bav(?:enida)?\.?\s+" + re.escape(normalize_text(alias)) + r"[^,]*,?",
                    "",
                    cleaned,
                )

        # Explicit other Brazilian states override homonymous neighborhoods
        other_codes = [c for c in BRAZIL_STATE_CODES if c not in allowed_state_codes]
        other_names = [BRAZIL_STATE_CODES[c] for c in other_codes]
        if re.search(
            r"\b(?:" + "|".join(re.escape(n) for n in other_names) + r")\b"
            r"|(?:[,/-]\s*)(?:" + "|".join(other_codes) + r")\b",
            cleaned,
        ):
            return "outside", None

        for city_rule in self.criteria.geography.allowed_cities:
            if any(contains_term(cleaned, alias) for alias in city_rule.aliases):
                return "allowed", city_rule.slug

        state_tokens = list(allowed_state_codes) + list(allowed_state_names)
        state_regex = r"\b(?:" + "|".join(re.escape(t) for t in state_tokens) + r")\b"

        if re.fullmatch(
            r"(?:" + "|".join(re.escape(t) for t in state_tokens) + r"|regiao_metropolitana_ampla|brasil|brazil)(?:\s*[,/-]?\s*(?:"
            + "|".join(re.escape(t) for t in state_tokens)
            + r"))*",
            cleaned.strip(),
        ):
            return "pending", None

        ambiguous_norm = {
            normalize_text(b) for b in self.criteria.geography.ambiguous_neighborhoods_without_city
        }
        for city_rule in self.criteria.geography.allowed_cities:
            for neighborhood in city_rule.neighborhoods:
                nb_norm = normalize_text(neighborhood)
                if contains_term(cleaned, nb_norm):
                    if nb_norm in ambiguous_norm:
                        return "pending", None
                    if re.search(state_regex, cleaned):
                        return "allowed", city_rule.slug
                    return "pending", None

        if "regiao_metropolitana_ampla" in cleaned:
            return "pending", None

        return "outside", None

    def _iter_affirmative_clauses(self, text: str) -> list[tuple[str, str]]:
        clauses: list[tuple[str, str]] = []
        optional_section = False
        for raw in re.split(r"[\n;]", text):
            n = normalize_text(raw).strip()
            if not n:
                continue
            if len(n) < 100 and re.match(
                r"^(?:desejaveis|diferenciais|nice to have|preferred qualifications|"
                r"o que aumenta (?:as )?suas chances|pode ser um diferencial|"
                r"sera (?:um |considerado um )?diferencial)",
                n,
            ):
                optional_section = True
                continue
            if len(n) < 100 and re.match(
                r"^(?:requisitos|responsabilidades|beneficios|nosso pacote|informacoes|"
                r"horario|jornada|obrigatorios|requirements|must have|what you.ll do)",
                n,
            ):
                optional_section = False
            if optional_section:
                continue
            if re.search(
                r"nao (?:e |sao )?(?:exigid|obrigator)|nao exig|dispensad|sem necessidade|"
                r"desejav|diferencial|preferencial|optional|preferred|nice to have|"
                r"not required|no experience required",
                n,
            ):
                continue
            clauses.append((raw.strip(), n))
        return clauses

    def _parse_experience_months(self, normalized_line: str) -> int | None:
        before = re.search(
            r"\b(\d+)(?:\s*[-a]\s*\d+)?\+?\s*(anos?|meses?|years?|months?)\s+(?:de\s+|of\s+)?(?:[\w-]+\s+){0,4}experien",
            normalized_line,
        )
        after = re.search(
            r"experien\w*.{0,30}?(\d+)(?:\s*[-a]\s*\d+)?\+?\s*(anos?|meses?|years?|months?)",
            normalized_line,
        )
        match = before or after
        if not match:
            return None
        amount = int(match.group(1))
        unit = match.group(2)
        return amount * (12 if unit.startswith(("ano", "year")) else 1)

    def _normalize_section_headers(self, description: str) -> str:
        adapted = re.sub(
            r"(?im)^(?:requirements|qualifications|what you bring|o que esperamos de voce|"
            r"o que esperamos de você|perfil que buscamos|habilidades essenciais|"
            r"o que voc[eê] precisa ter).*?$",
            "Requisitos",
            description,
        )
        adapted = re.sub(
            r"(?im)^(?:responsibilities|what you.ll do|como ser[aá] seu dia a dia|"
            r"suas principais entregas|principais responsabilidades|your responsibilities).*?$",
            "Responsabilidades",
            adapted,
        )
        adapted = re.sub(
            r"(?im)^(?:o que aumenta (?:as )?suas chances|ser[aá] considerado um diferencial|"
            r"diferenciais.*|nice to have|preferred qualifications).*?$",
            "Desejáveis",
            adapted,
        )
        adapted = re.sub(
            r"(?im)^.*(?:pacote de remunera[çc][aã]o|nosso pacote de benef[ií]cios|what we offer|"
            r"bolsa e benef[ií]cios|o que oferecemos|empresa oferece|informa[çc][oõ]es adicionais).*$",
            "Benefícios",
            adapted,
        )
        return adapted

    def _extract_requirements(
        self, description: str
    ) -> tuple[list[MatchedRequirement], list[UnmappedRequirement], list[str]]:
        adapted = self._normalize_section_headers(description)
        norm_full = normalize_text(adapted)
        relevant_entries = [
            (tag, variants, pat)
            for (tag, variants, pat) in self._skill_terms
            if pat.search(norm_full)
        ]

        matched_reqs: list[MatchedRequirement] = []
        unmapped: list[UnmappedRequirement] = []
        section: Literal["mandatory", "desirable", "responsibility", "info", "unspecified"] = "unspecified"

        for raw in adapted.splitlines():
            line = raw.strip(" -*•#\t")
            if not line:
                continue
            n = normalize_text(line)
            if len(line) < 75:
                if any(h in n for h in ("responsabilidades", "atribuicoes")):
                    section = "responsibility"
                    continue
                if any(h in n for h in ("desejaveis", "desejavel", "diferenciais")):
                    section = "desirable"
                    continue
                if any(h in n for h in ("requisitos", "qualificacoes", "obrigatorios")):
                    section = "mandatory"
                    continue
            if n.startswith(("beneficios", "informacoes do cargo", "codigo da vaga", "data de")):
                section = "info"
                continue
            if section == "info":
                continue

            if re.search(r"nao (?:e |sao )?(?:exig|obrig)|nao exig|sem necessidade|dispensad", n):
                kind: Literal["mandatory", "desirable", "responsibility", "optional", "unspecified"] = "optional"
            elif re.search(r"desejav|diferencial|preferencial|nice to have", n):
                kind = "desirable"
            elif section in {"mandatory", "desirable", "responsibility"}:
                kind = section
            else:
                kind = "unspecified"

            line_matched = False
            for tag, variants, _ in relevant_entries:
                norm_tag = normalize_text(tag)
                if norm_tag == "office" and not re.search(
                    r"\boffice\b", re.sub(r"home[ -]?office", "", n)
                ):
                    continue
                if norm_tag.startswith("redes") and re.search(r"redes sociais|social networks", n):
                    continue

                terms = [v for v in variants if v.strip() and contains_term(line, v)]
                if not terms:
                    continue
                line_matched = True

                matched_reqs.append(
                    MatchedRequirement(
                        skill_name=tag,
                        matched_keywords=list(dict.fromkeys(terms)),
                        requirement_kind=kind,
                        excerpt=line,
                    )
                )

            if not line_matched and kind in {"mandatory", "desirable", "responsibility"} and len(line) > 3:
                if n not in {"conhecimentos:", "conhecimentos"}:
                    unmapped.append(UnmappedRequirement(text=line, requirement_kind=kind))

        if any(r.requirement_kind in {"mandatory", "desirable", "responsibility"} for r in matched_reqs):
            matched_reqs = [r for r in matched_reqs if r.requirement_kind != "unspecified"]

        return matched_reqs, unmapped, [tag for (tag, _, _) in relevant_entries]

    def _evaluate_method_and_automation(
        self, job: RawJobInput
    ) -> dict[str, Any]:
        text = job.description or ""
        norm_desc = normalize_text(text)

        process_lines = [
            line
            for line in text.splitlines()
            if re.search(
                r"etapas|processo seletivo|video.?curriculo|video obrigatorio|dinamica|testes|"
                r"cadastro|formulario|envie.{0,25}curriculo|curriculo.{0,25}(?:email|e-mail|pdf)|anex|upload",
                normalize_text(line),
            )
        ]
        hard_process = [
            line
            for line in process_lines
            if re.search(
                r"video.?curriculo|video obrigatorio|dinamica|multiplos testes|varias etapas|"
                r"processo.{0,15}(?:longo|extenso)|\b[5-9] etapas",
                normalize_text(line),
            )
        ]

        method: Literal["A", "B", "C", "D", "desconhecido"] = "desconhecido"
        effort: Literal["baixo", "medio", "alto", "desconhecido"] = "desconhecido"

        if hard_process or len(job.process_steps) >= 5:
            method, effort = "D", "alto"
        elif job.source_method_hint:
            method = job.source_method_hint
            effort = job.source_effort_hint or "desconhecido"
        else:
            email_match = EMAIL_RE.search(norm_desc)
            if (email_match or re.search(r"curriculo.{0,100}(?:e-mail|email)", norm_desc)) and re.search(
                r"(?:envie|enviar|encaminh|curriculos para|candidatura).{0,70}curriculo|curriculo.{0,100}(?:email|e-mail|@)",
                norm_desc,
            ):
                method, effort = "A", "baixo"
            elif job.pdf_confirmed and job.requires_registration:
                method, effort = "B", "medio"
            elif job.pdf_confirmed and job.short_form:
                method, effort = "A", "baixo"
            elif re.search(
                r"preench[ae].{0,25}formulario|cadastro simples|perguntas de candidatura",
                norm_desc,
            ):
                method, effort = "C", "medio"
            elif re.search(r"cadastro extenso|multiplos testes", norm_desc):
                method, effort = "D", "alto"

        pdf_status: Literal["confirmado", "verificar", "desconhecido"] = (
            "confirmado"
            if job.pdf_confirmed
            else "verificar"
            if method in {"A", "B"}
            else "desconhecido"
        )

        # Platform & 3-Tier Automation Capability (technical block NEVER discards job!)
        target_str = normalize_text(f"{job.url} {job.source}")
        platform_name = next(
            (name for _, (marker, name) in PLATAFORMAS_LOGIN.items() if marker in target_str),
            None,
        )
        needs_login = job.requires_login or bool(platform_name)
        is_incomplete = job.incomplete_summary or len(text.strip()) < 80
        is_blocked = job.access_blocked
        is_complex = method == "D" or len(job.process_steps) >= 4

        email_found = EMAIL_RE.search(text)
        channel = job.public_channel or (email_found.group(0).rstrip(".") if email_found else None)
        direct_email = bool(channel and "@" in channel and not needs_login and not is_blocked and not is_incomplete)

        reasons: list[str] = []
        if direct_email:
            capability: Literal[
                "automacao_possivel",
                "candidatura_assistida",
                "apenas_descoberta",
                "necessita_intervencao_manual",
                "desconhecida",
            ] = "automacao_possivel"
            tier: Literal[1, 2, 3] = 1
            reasons.append("Direct application email published in announcement; full local prep supported.")
            instructions = [
                f"Anexar o PDF padronizado ao e-mail {channel}.",
                "Envio permanece sob confirmação manual do usuário.",
            ]
        elif (is_blocked or needs_login) and is_incomplete:
            capability = "necessita_intervencao_manual"
            tier = 3
            reasons.append("Incomplete description behind login or anti-bot gate; user can paste full text.")
            instructions = [
                f"Abrir {job.url} no navegador e colar a descrição completa na Importação Assistida.",
                "Limitação técnica do portal não descarta a vaga.",
            ]
        elif is_blocked:
            capability = "candidatura_assistida"
            tier = 2
            reasons.append("Site restricts automated scraping; manual submission with generated PDF.")
            instructions = [
                f"Abrir {job.url} e anexar manualmente o PDF gerado.",
                "Nenhuma tentativa de contornar bloqueio ou CAPTCHA é executada.",
            ]
        elif needs_login or is_complex:
            capability = "candidatura_assistida"
            tier = 2
            reasons.append(
                f"Authenticated submission flow on {platform_name}"
                if platform_name
                else "Multi-step candidate portal submission"
            )
            instructions = [
                f"Abrir {job.url}, acessar conta existente e anexar o PDF preparado.",
            ]
        elif is_incomplete:
            capability = "apenas_descoberta"
            tier = 3
            reasons.append("Summary-only listing; full requirements require opening the source link.")
            instructions = [
                f"Abrir {job.url} para conferir os detalhes completos ou importar a descrição.",
            ]
        else:
            capability = "candidatura_assistida"
            tier = 2
            reasons.append("Public listing ready for local resume adaptation and manual submission.")
            instructions = [f"Abrir {job.url} e concluir a candidatura manualmente com o PDF preparado."]

        return {
            "method": method,
            "effort": effort,
            "pdf_status": pdf_status,
            "tier": tier,
            "capability": capability,
            "platform_name": platform_name,
            "instructions": instructions,
            "reasons": reasons,
        }

    def evaluate(self, job: RawJobInput) -> TriageResult:
        # Strip unfilled bracket placeholders such as '[Presencial/Híbrido/Remoto]'
        clean_desc = re.sub(r"\[[^\]\n]*\]", "", job.description or "").split(
            "Mais Oportunidades:", 1
        )[0]
        norm_desc = normalize_text(clean_desc)
        norm_title = normalize_text(job.title)
        norm_loc = normalize_text(job.location or "")

        hard_incompatibilities: list[str] = []
        blocking_pendencies: list[str] = []
        operational_omissions: list[str] = []
        technical_notes: list[str] = []
        strategic_exclusions: list[str] = []

        # 1. Blocked companies & recruiting-on-behalf check
        job_company_norm = _normalize_company_name(job.company)
        for entity in self.blocked.companies:
            blocked_names = [
                entity.display_name,
                *( [entity.corporate_name] if entity.corporate_name else [] ),
                *entity.aliases,
                *(rel.name for rel in entity.confirmed_related_entities),
            ]
            for name in filter(None, blocked_names):
                target_norm = _normalize_company_name(name)
                acting_on_behalf = bool(
                    re.search(
                        r"(?:em nome d[aeo]|recrutando para|contratando para)\s+"
                        + re.escape(normalize_text(name))
                        + r"(?!\w)",
                        norm_desc,
                    )
                )
                if acting_on_behalf or (job_company_norm and job_company_norm == target_norm):
                    hard_incompatibilities.append(f"Empresa bloqueada: {entity.display_name}")
                    break

        if job.company_relation_uncertain:
            blocking_pendencies.append("Investigar relação societária incerta com empresa bloqueada")
        if not job.company:
            operational_omissions.append("Confirmar identidade do empregador")

        # 2. Strategic exclusions (URLs, disabled channels like CIEE, massive programs)
        clean_url_base = job.url.split("?")[0]
        for rule in self.criteria.strategic_exclusions:
            if clean_url_base in rule.urls:
                strategic_exclusions.append(rule.reason)
            for term in rule.excluded_source_terms:
                if contains_term(job.source, term) or contains_term(job.company or "", term):
                    strategic_exclusions.append(rule.reason)

        # 3. Workplace Mode & Geography
        mode: str | None = job.workplace_mode
        if not mode:
            header_context = f"{norm_loc} {norm_title}"
            if re.search(r"hibrid|hybrid", header_context):
                mode = "hibrido"
            elif re.search(r"remot|home.?office", header_context):
                mode = "remoto"
            elif re.search(r"presencial|on.?site", header_context):
                mode = "presencial"
            else:
                for raw_line in clean_desc.splitlines():
                    ln = normalize_text(raw_line)
                    if re.search(r"auxilio|beneficio|apenas para contratos", ln):
                        continue
                    if re.search(
                        r"(?:modelo|modalidade|trabalho|atuacao|vaga|posicao|workplace).{0,35}(?:remot|home office)|100%\s*remot|#?li-remote",
                        ln,
                    ):
                        mode = "remoto"
                        break
                    if re.search(
                        r"#?li-hybrid|(?:modelo|modalidade|trabalho|atuacao|vaga|posicao).{0,35}hibrid",
                        ln,
                    ):
                        mode = "hibrido"
                        break
                    if re.search(
                        r"#?li-onsite|(?:trabalho|atuacao|vaga|modelo|modalidade|disponibilidade|rotina|atuar).{0,35}presencial",
                        ln,
                    ):
                        mode = "presencial"
                        break

        geo_state, matched_city = self.evaluate_geography(job.location or clean_desc[:200] if not job.location and any(contains_term(clean_desc, c.canonical_name) for c in self.criteria.geography.allowed_cities) else job.location)
        if not mode:
            operational_omissions.append("Confirmar modalidade de trabalho")
            if geo_state == "outside" and job.location:
                operational_omissions.append(
                    "Cidade cadastral fora dos municípios permitidos, sem exigência presencial confirmada"
                )
        elif mode in {"presencial", "hibrido"}:
            if geo_state == "outside":
                hard_incompatibilities.append("Presencial/híbrido fora dos municípios permitidos")
            elif geo_state == "pending":
                operational_omissions.append("Confirmar município de trabalho permitido")
        elif mode == "remoto":
            if re.search(
                r"(?:only|apenas|somente|exclusiv\w*).{0,30}(?:us residents|united states|residentes nos eua|residentes em portugal)",
                f"{norm_desc}\n{norm_loc}",
            ):
                hard_incompatibilities.append("Remoto restrito a residentes fora do Brasil")
            elif not (
                job.country == "BR"
                or contains_term(norm_loc, "brasil")
                or contains_term(norm_loc, "brazil")
                or geo_state == "allowed"
                or re.search(r"residentes? no brasil|residir no brasil|anywhere in the world|worldwide", norm_desc)
            ):
                operational_omissions.append("Confirmar aceitação de residentes no Brasil")

        # 4. Contract Policy
        contract_corpus = "\n".join(filter(None, [job.contract_hint, job.title, clean_desc]))
        allowed_contracts: set[str] = set()
        rejected_contracts: set[str] = set()
        for raw_clause, norm_clause in self._iter_affirmative_clauses(contract_corpus):
            scrubbed = re.sub(
                r"nao (?:aceita |e |contrata )?(?:pj|mei|freelancer|noturno|noturna)",
                "",
                norm_clause,
            )
            for bad_term in ("pj", "mei", "freelancer", "freelance", "voluntario", "voluntaria", "voluntariado", "independent contractor"):
                if contains_term(scrubbed, bad_term):
                    rejected_contracts.add(bad_term)
            if re.search(r"nao remunerad[oa]|sem remuneracao|unpaid", scrubbed):
                rejected_contracts.add("nao_remunerado")
            if contains_term(scrubbed, "clt"):
                allowed_contracts.add("clt")
            if re.search(r"(?:contrato|vinculo|regime|tipo).{0,20}(?:aprendiz|aprendizagem)", scrubbed) or (
                raw_clause == job.title and re.search(r"jovem aprendiz|\bapprentice\b", scrubbed)
            ):
                allowed_contracts.add("aprendiz")

        is_internship = bool(
            re.search(r"estagio|estagiari|\bintern(?:ship)?\b", f"{norm_title}\n{normalize_text(job.contract_hint or '')}")
            or re.search(r"(?:contrato|vinculo|regime|tipo).{0,20}estagio", norm_desc)
        )
        if is_internship:
            if not self.criteria.contracts.require_explicit_paid_internship_signal or re.search(
                r"bolsa|estagio remunerado|paid internship|remuneracao", norm_desc
            ):
                allowed_contracts.add("estagio_remunerado")

        if rejected_contracts and not allowed_contracts:
            hard_incompatibilities.append("Vínculo contratual excluído ou oportunidade não remunerada")
        elif rejected_contracts:
            blocking_pendencies.append("Anúncio menciona vínculos permitidos e excluídos; confirmar regime CLT/estágio")
        elif not allowed_contracts:
            operational_omissions.append("Confirmar vínculo contratual permitido e natureza remunerada")

        # 5. Schedule / Shift Policy
        schedule_confirmed = False
        earliest_min = self.criteria.schedule.earliest_hour * 60
        latest_min = self.criteria.schedule.latest_hour * 60
        soft_latest_min = self.criteria.schedule.soft_latest_hour * 60

        for _, norm_clause in self._iter_affirmative_clauses(f"{job.title}\n{clean_desc}"):
            if re.search(r"horario|jornada|turno|expediente|escala|\b\d{1,2}(?:h|:\d{2})\s*(?:as|a|-)", norm_clause):
                scrubbed = re.sub(r"nao (?:ha |e |tem )?(?:turno )?noturn[oa]", "", norm_clause)
                if self.criteria.schedule.reject_night_or_overnight and re.search(
                    r"\bnoturn[oa]\b|madrugada|night shift|overnight", scrubbed
                ):
                    hard_incompatibilities.append("Turno noturno/madrugada excluído")
                for h1, m1, h2, m2 in re.findall(
                    r"\b(\d{1,2})(?:(?:h|:)(\d{2})?)?h?\s*(?:as|a|-)\s*(\d{1,2})(?:h|:)(\d{2})?h?\b",
                    scrubbed,
                ):
                    start_m = int(h1) * 60 + int(m1 or 0)
                    end_m = int(h2) * 60 + int(m2 or 0)
                    if start_m < 1440 and end_m < 1440:
                        schedule_confirmed = True
                        if start_m < earliest_min or end_m > soft_latest_min or end_m <= start_m:
                            hard_incompatibilities.append("Jornada alcança horário noturno/madrugada")
                        elif end_m > latest_min:
                            operational_omissions.append("Confirmar encerramento diurno da jornada")
                if re.search(r"diurno|horario comercial|turno da manha|turno da tarde|segunda a sexta", scrubbed):
                    schedule_confirmed = True

        if not schedule_confirmed:
            operational_omissions.append("Confirmar horário diurno")

        # 6. Validity & Deadlines
        validity = job.validity_status
        if job.official_deadline:
            try:
                dl = datetime.fromisoformat(job.official_deadline.replace("Z", "+00:00"))
                if dl.tzinfo and dl < datetime.now(timezone.utc):
                    validity = "encerrada"
                    hard_incompatibilities.append("Prazo oficial de candidatura expirado")
            except ValueError:
                operational_omissions.append("Confirmar data limite de inscrição")
        if validity == "encerrada":
            hard_incompatibilities.append("Vaga encerrada na fonte")

        # 7. Requirements, Short-Prep Plans, & Mandatory Experience Threshold
        reqs, unmapped, _ = self._extract_requirements(clean_desc)
        short_plans: list[ShortPrepPlanItem] = []
        preparable_names_list: list[str] = []

        for rule in self.criteria.short_prep_rules:
            matching_lines = [
                ln for ln in clean_desc.splitlines() if any(contains_term(ln, a) for a in rule.aliases)
            ]
            has_prereq = any(
                normalize_text(pr).replace(" ", "_") in self._norm_skill_tokens
                or normalize_text(pr) in self._norm_skill_tokens
                for pr in rule.prerequisite_skill_ids
            )
            if not matching_lines or not has_prereq:
                continue
            if any(
                re.search(r"dominio avancado|especialista|producao.{0,15}(?:obrigator|exigid)", normalize_text(ln))
                for ln in matching_lines
            ):
                technical_notes.append(f"Avaliar escopo avançado exigido de {rule.term}")
                continue
            already_mapped = any(
                any(contains_term(r.skill_name, a) for a in rule.aliases) for r in reqs
            )
            if not already_mapped:
                preparable_names_list.append(rule.term)
                short_plans.append(
                    ShortPrepPlanItem(term=rule.term, study_plan=rule.study_plan, acquired=False)
                )

        max_exp_months = self.criteria.career_focus.max_mandatory_experience_months
        for raw_clause, norm_clause in self._iter_affirmative_clauses(clean_desc):
            months = self._parse_experience_months(norm_clause)
            if months is not None:
                is_corporate_history = bool(
                    re.search(r"(?:empresa|companhia|organizacao).{0,35}(?:tem|possui|com).{0,20}experiencia", norm_clause)
                )
                is_explicit_min = bool(
                    re.search(
                        r"minim|obrigator|pelo menos|at least|minimum|required|\d+\+\s*years?|experiencia\s+de\s+\d+",
                        norm_clause,
                    )
                )
                if months >= max_exp_months and is_explicit_min and not is_corporate_history:
                    hard_incompatibilities.append(
                        f"Experiência mínima explícita ({months} meses) igual ou superior ao limite ({max_exp_months} meses)"
                    )
                elif months > 2 and not is_corporate_history:
                    technical_notes.append(f"Avaliar requisito de experiência intermediária: {raw_clause}")

            if re.search(r"certifica[cd]|certificado|certification", norm_clause) and re.search(
                r"obrigator|indispens|must|required", norm_clause
            ):
                blocking_pendencies.append(f"Confirmar certificação obrigatória: {raw_clause}")

            is_inclusive = bool(
                re.search(r"todas as vagas|tambem sao destinadas|independentemente", norm_clause)
            )
            if (
                not is_inclusive
                and re.search(r"exclusiv|vaga afirmativa|somente|apenas", norm_clause)
                and re.search(r"pcd|deficiencia|mulheres|pessoas negras|indigen", norm_clause)
            ):
                blocking_pendencies.append(f"Confirmar elegibilidade de vaga afirmativa/exclusiva: {raw_clause}")

        # 8. Area Scoring & Seniority Tier (Driven by SearchCriteria.role_catalog)
        configured_areas = [
            a for a in self.criteria.role_catalog.area_keywords if a != "ti_geral"
        ]
        area_scores: dict[str, int] = {area: 0 for area in configured_areas}

        for cat_area, keywords in self.criteria.role_catalog.area_keywords.items():
            title_hits = sum(1 for kw in keywords if contains_term(job.title, kw))
            desc_hits = sum(1 for kw in keywords if contains_term(clean_desc, kw))
            if title_hits and cat_area in area_scores:
                area_scores[cat_area] += 30 * title_hits
            elif title_hits and cat_area == "ti_geral" and "suporte" in area_scores:
                if not any(v > 0 for v in area_scores.values()):
                    area_scores["suporte"] += 20
            if desc_hits and cat_area in area_scores:
                area_scores[cat_area] += min(15, 3 * desc_hits)

        best_score = max(area_scores.values()) if area_scores else 0
        winners = [a for a, sc in area_scores.items() if sc == best_score and sc > 0]
        selected_area = winners[0] if len(winners) == 1 else (winners[0] if winners else None)
        if not winners:
            operational_omissions.append("Área temática identificada de forma geral")

        is_senior = any(
            contains_term(job.title, sig)
            for sig in self.criteria.career_focus.out_of_focus_seniority_signals
        )
        is_entry = any(
            contains_term(job.title, sig)
            for sig in self.criteria.career_focus.entry_level_signals
        )
        career_tier: Literal["entrada", "avaliar", "fora_foco"] = (
            "fora_foco" if is_senior else "entrada" if is_entry else "avaliar"
        )

        # 9. Automation & Method Axis
        auto = self._evaluate_method_and_automation(job)
        if job.massive_program and job.company_size == "gigante" and auto["effort"] == "alto":
            strategic_exclusions.append("Programa massivo extenso fora da estratégia ativa")

        if job.incomplete_summary or len(clean_desc.strip()) < 80:
            operational_omissions.append("Obter descrição integral para avaliação completa de requisitos")

        matched_names = sorted({r.skill_name for r in reqs})
        preparable_names = sorted(set(preparable_names_list))

        # Relevance & Practical Priority Scoring
        relevance = min(
            100,
            (35 if is_entry else 10)
            + (25 if selected_area else 0)
            + min(30, len(matched_names) * 5)
            + (10 if validity == "confirmadamente_aberta" else 0),
        )
        if hard_incompatibilities:
            relevance = 0
        elif is_senior:
            relevance = min(relevance, 10)

        practical = relevance + {"A": 20, "B": 15, "C": 0, "D": -20}.get(auto["method"], 0)
        if geo_state == "allowed":
            practical += 5

        # Independent Compatibility, Operational Status, & Classification
        hard_incompatibilities = sorted(set(hard_incompatibilities))
        strategic_exclusions = sorted(set(strategic_exclusions))
        blocking_pendencies = sorted(set(blocking_pendencies))
        operational_omissions = sorted(set(operational_omissions))
        technical_notes = sorted(set(technical_notes))

        if hard_incompatibilities:
            prof_compat: Literal[
                "compativel",
                "compativel_com_preparacao",
                "insuficiente_para_avaliar",
                "incompativel_por_impedimento_concreto",
            ] = "incompativel_por_impedimento_concreto"
        elif job.incomplete_summary or (not matched_names and not preparable_names and len(clean_desc.strip()) < 80):
            prof_compat = "insuficiente_para_avaliar"
        elif preparable_names or technical_notes:
            prof_compat = "compativel_com_preparacao"
        else:
            prof_compat = "compativel"

        if validity == "encerrada" or hard_incompatibilities:
            op_status: Literal[
                "pronta_para_preparar_candidatura",
                "necessita_confirmar_informacao_importante",
                "necessita_intervencao_usuario",
                "encerrada_ou_indisponivel",
            ] = "encerrada_ou_indisponivel"
        elif auto["tier"] == 3 or blocking_pendencies:
            op_status = "necessita_intervencao_usuario"
        elif operational_omissions:
            op_status = "necessita_confirmar_informacao_importante"
        else:
            op_status = "pronta_para_preparar_candidatura"

        allow_omissions = self.criteria.omissions_policy.allow_local_prep_with_operational_omissions
        local_prep_allowed = (
            not hard_incompatibilities
            and not strategic_exclusions
            and not is_senior
            and not blocking_pendencies
            and selected_area is not None
            and (allow_omissions or not operational_omissions)
        )

        if hard_incompatibilities:
            classification: Literal[
                "aprovada",
                "aprovada_com_preparacao",
                "elegivel_para_avaliacao",
                "pendente",
                "descartada",
                "fora_foco",
                "fora_estrategia",
            ] = "descartada"
        elif strategic_exclusions:
            classification = "fora_estrategia"
        elif is_senior:
            classification = "fora_foco"
        elif blocking_pendencies or (operational_omissions and not allow_omissions) or not selected_area or job.incomplete_summary:
            classification = "pendente"
        elif technical_notes:
            classification = "elegivel_para_avaliacao"
        elif preparable_names:
            classification = "aprovada_com_preparacao"
        else:
            classification = "aprovada"

        initial_stage: Literal["descoberta", "encerrada_ou_descartada"] = (
            "encerrada_ou_descartada"
            if hard_incompatibilities or strategic_exclusions
            else "descoberta"
        )

        human_labels = build_job_human_labels(
            classification=classification,
            compatibility=prof_compat,
            operational_status=op_status,
            automation_capability=auto["capability"],
            stage=initial_stage,
            workplace_mode=mode,
            career_tier=career_tier,
            service_tier=auto["tier"],
        )

        return TriageResult(
            labels=human_labels,
            classification=classification,
            professional_compatibility=prof_compat,
            local_prep_allowed=local_prep_allowed,
            area_id=selected_area,
            candidate_areas=winners,
            area_scores=area_scores,
            career_tier=career_tier,
            relevance_score=relevance,
            practical_priority=practical,
            hard_incompatibilities=hard_incompatibilities,
            strategic_exclusions=strategic_exclusions,
            blocking_pendencies=blocking_pendencies,
            operational_omissions=operational_omissions,
            technical_review_notes=technical_notes,
            operational_status=op_status,
            inferred_workplace_mode=mode,
            inferred_contract="/".join(sorted(allowed_contracts or rejected_contracts)) or None,
            matched_city_slug=matched_city,
            geography_state=geo_state,
            matched_skills=matched_names,
            preparable_gaps=preparable_names,
            requirements=reqs,
            short_prep_plans=short_plans,
            unmapped_requirements=unmapped,
            application_method=auto["method"],
            application_effort=auto["effort"],
            pdf_status=auto["pdf_status"],
            service_tier=auto["tier"],
            automation_capability=auto["capability"],
            platform_name=auto["platform_name"],
            submission_instructions=auto["instructions"],
            automation_reasons=auto["reasons"],
            initial_application_stage=initial_stage,
        )
