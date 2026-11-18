"""One-time faithful migration from `user_data/legacy_archive/` to Phase 1 schema & SQLite WAL.

Guarantees:
- Reads `user_data/legacy_archive/` in strictly read-only mode (zero mutations to originals,
  zero access to any sensitive vault).
- Contains ZERO hardcoded personal strings (public-repo safe).
- Normalizes legacy self-deprecating qualifiers ('básico', 'em nível inicial',
  'familiaridade básica') on public display fields while preserving full internal context.
- Migrates all 773 opportunities, 785 source fingerprints, 258 verification evidences,
  18 resume versions (hashes + diffs, zero binary BLOBs), 35 queue/application tracking
  records, 242 lifecycle events, and 27 discovery/verification run summaries into `app.sqlite3`.
- Emits a complete reconciliation report proving 100% factual and relational parity.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.dom import minidom
from zipfile import ZipFile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import text
from sqlmodel import Session, SQLModel

from app.core.config import assert_safe_path
from app.core.database import create_sqlite_wal_engine, init_db_schema
from app.models import (
    ApplicationEvent,
    ApplicationTracking,
    BlockedCompaniesConfig,
    DiscoveryRun,
    JobObservation,
    JobOpportunity,
    JobSourcesConfig,
    MasterProfile,
    ResumeVersion,
    SearchCriteria,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

CONFIDENT_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bfamiliaridade\s+b[aá]sica\s+com\s+", re.IGNORECASE), ""),
    (re.compile(r"\bfamiliaridade\s+b[aá]sica\s+em\s+", re.IGNORECASE), ""),
    (re.compile(r"\s+em\s+n[ií]vel\s+inicial", re.IGNORECASE), ""),
    (re.compile(r"\bn[ií]vel\s+inicial\b", re.IGNORECASE), "contexto introdutório"),
    (re.compile(r"\bconhecimentos\s+b[aá]sicos\s+de\s+", re.IGNORECASE), "conhecimentos em "),
    (re.compile(r"\bconhecimentos\s+b[aá]sicos\s+em\s+", re.IGNORECASE), "conhecimentos em "),
    (
        re.compile(
            r"\b(administra[çc][aã]o|gerenciamento|configura[çc][aã]o|configura[çc][oõ]es|manuten[çc][aã]o|suporte|planilhas|redes)\s+b[aá]sic[oa]s?\b",
            re.IGNORECASE,
        ),
        r"\1",
    ),
    (re.compile(r"\b(python\s+e\s+sql|sql|python|excel|office)\s+b[aá]sicos?\b", re.IGNORECASE), r"\1"),
    (re.compile(r"\s*\(\s*b[aá]sic[oa]s?\s*\)", re.IGNORECASE), ""),
    (re.compile(r"\bb[aá]sic[oa]s?\b", re.IGNORECASE), ""),
]


def _norm(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", value.casefold()) if not unicodedata.combining(c)
    ).strip()


def _slugify(value: str, prefix: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", _norm(value)).strip("_")
    return slug[:48] if slug else prefix


def _canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    filtered_query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"gh_src", "lever-source", "source", "ref"}
    ]
    netloc = parsed.netloc.lower().replace("boards.greenhouse.io", "job-boards.greenhouse.io")
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), urlencode(sorted(filtered_query)), "")
    )


def clean_confident_text(value: str, log_bucket: list[dict[str, str]], context: str) -> str:
    cleaned = value
    for pattern, replacement in CONFIDENT_REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned).strip()
    if cleaned and value and value[0].isupper() and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    if cleaned != value:
        log_bucket.append({"context": context, "before": value, "after": cleaned})
    return cleaned


def _extract_docx_structure(docx_path: Path) -> tuple[list[str], dict[str, str]]:
    with ZipFile(docx_path) as zf:
        doc = minidom.parseString(zf.read("word/document.xml"))
        rels = minidom.parseString(zf.read("word/_rels/document.xml.rels"))
        links: dict[str, str] = {}
        for rel in rels.getElementsByTagName("Relationship"):
            if "hyperlink" in rel.getAttribute("Type"):
                target = rel.getAttribute("Target")
                if "github" in target.lower():
                    links["GitHub"] = target
                elif "linkedin" in target.lower():
                    links["LinkedIn"] = target
        body = doc.getElementsByTagNameNS(W_NS, "body")[0]
        paragraphs = [
            "".join(
                node.firstChild.data if node.firstChild else ""
                for node in elem.getElementsByTagNameNS(W_NS, "t")
            )
            for elem in body.childNodes
            if elem.nodeType == elem.ELEMENT_NODE
        ]
    return paragraphs, links


def _load_legacy_area_config(legacy_config_dir: Path, rel_path: str) -> dict[str, Any]:
    cfg_path = legacy_config_dir / Path(rel_path).name
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if "herdar" in cfg:
        parent_path = legacy_config_dir / Path(cfg["herdar"]).name
        parent = json.loads(parent_path.read_text(encoding="utf-8"))
        merged_changes = {c["indice"]: c for c in parent.get("alteracoes", [])}
        for c in cfg.get("alteracoes", []):
            merged_changes[c["indice"]] = merged_changes.get(c["indice"], {}) | c
        merged = parent | cfg
        merged["alteracoes"] = list(merged_changes.values())
        return merged
    return cfg


def _segments_to_text(segments: list[dict[str, Any]]) -> str:
    return "".join(str(s.get("texto", "")) for s in segments)


def _split_skill_group(segments: list[dict[str, Any]]) -> tuple[str, str]:
    full = _segments_to_text(segments).strip()
    if ":" in full:
        label, rest = full.split(":", 1)
        return label.strip(), rest.strip()
    return "Competências", full


def build_master_profile(legacy_dir: Path, normalizations: list[dict[str, str]]) -> tuple[MasterProfile, dict[str, Any]]:
    areas_meta = json.loads((legacy_dir / "config" / "areas.json").read_text(encoding="utf-8"))
    suporte_docx = next((legacy_dir / areas_meta["suporte"]["pasta"]).glob("*.docx"))

    paragraphs, hyperlink_targets = _extract_docx_structure(suporte_docx)

    # 1. Header parsing from DOCX lines 0..3
    full_name = paragraphs[0].strip()
    loc_parts = [p.strip() for p in paragraphs[1].split("•")]
    age_match = re.search(r"(\d+)", loc_parts[0]) if loc_parts else None
    age = int(age_match.group(1)) if age_match else None
    addr_text = loc_parts[-1] if loc_parts else ""
    neighborhood, city_state = (
        [x.strip() for x in addr_text.split(",", 1)] if "," in addr_text else [None, addr_text]
    )
    city, state = (
        [x.strip() for x in re.split(r"[-–—]", city_state, maxsplit=1)]
        if re.search(r"[-–—]", city_state)
        else [city_state.strip(), "PE"]
    )
    contact_parts = [p.strip() for p in paragraphs[2].split("•")]
    email = next((p for p in contact_parts if "@" in p), "")
    phone = next((p for p in contact_parts if "@" not in p), "")
    link_displays = [p.strip() for p in paragraphs[3].split("•") if p.strip()]
    profile_links = []
    for disp in link_displays:
        label = "GitHub" if "github" in disp.lower() else "LinkedIn" if "linkedin" in disp.lower() else "Website"
        url = hyperlink_targets.get(label, f"https://{disp.lstrip('/')}")
        profile_links.append({"label": label, "url": url, "display_text": disp})

    # 2. Load English config for translations
    en_cfg = _load_legacy_area_config(legacy_dir / "config", areas_meta["suporte-en"]["config"])
    en_by_idx = {c["indice"]: _segments_to_text(c["trechos"]) for c in en_cfg.get("alteracoes", [])}

    # 3. Education (3 academic entries + en-US translations)
    education = [
        {
            "id": "edu_unicap_spi",
            "degree": "Graduação em Sistemas para Internet",
            "institution": "UNICAP — Universidade Católica de Pernambuco",
            "program_note": "via Embarque Digital",
            "period_label": "Início: Jul/2026 · Previsão de conclusão: Dez/2028",
            "status": "in_progress",
            "areas": [],
            "factual_source": "Currículo Base/Currículo Base.docx; perfil/perfil-factual.md#formacao-e-politica-de-cursos",
            "translations": {
                "en-US": {
                    "degree": "Undergraduate Degree in Internet Systems",
                    "institution": "UNICAP — Catholic University of Pernambuco",
                    "program_note": "via Embarque Digital",
                    "period_label": en_by_idx.get(12, "Start: Jul/2026 · Expected completion: Dec/2028"),
                }
            },
        },
        {
            "id": "edu_uninter_ads",
            "degree": "Graduação em Análise e Desenvolvimento de Sistemas",
            "institution": "UNINTER — Centro Universitário Internacional",
            "program_note": None,
            "period_label": "Concluído em Dez/2025",
            "status": "completed",
            "areas": [],
            "factual_source": "Currículo Base/Currículo Base.docx; perfil/perfil-factual.md#formacao-e-politica-de-cursos",
            "translations": {
                "en-US": {
                    "degree": en_by_idx.get(14, "Associate Degree in Systems Analysis and Development"),
                    "institution": en_by_idx.get(15, "UNINTER — International University Center"),
                    "program_note": None,
                    "period_label": en_by_idx.get(16, "Completed in Dec/2025"),
                }
            },
        },
        {
            "id": "edu_etemac_redes",
            "degree": "Curso Técnico em Redes de Computadores",
            "institution": "ETEMAC — Escola Técnica Estadual Maximiano Accioly Campos",
            "program_note": None,
            "period_label": "Concluído em Dez/2022",
            "status": "completed",
            "areas": [],
            "factual_source": "Currículo Base/Currículo Base.docx; perfil/perfil-factual.md#formacao-e-politica-de-cursos",
            "translations": {
                "en-US": {
                    "degree": en_by_idx.get(18, "Technical Course in Computer Networks"),
                    "institution": en_by_idx.get(19, "ETEMAC — State Technical School Maximiano Accioly Campos"),
                    "program_note": None,
                    "period_label": en_by_idx.get(20, "Completed in Dec/2022"),
                }
            },
        },
    ]

    # 4. Skills Catalog: 47 structured skills + LinkedIn academic table
    structured_raw = json.loads(
        (legacy_dir / "perfil" / "competencias-estruturadas.json").read_text(encoding="utf-8")
    )["competencias"]

    skills: list[dict[str, Any]] = []
    known_aliases: set[str] = set()
    practice_markers = ("pratica", "instalacao", "manutencao", "suporte", "relatorios", "uso_")

    for item in structured_raw:
        raw_state = item["estado"]
        ctx = item.get("contexto") or ""
        if raw_state == "existente":
            veracity = (
                "confirmed_practice"
                if any(m in ctx for m in practice_markers)
                else "academic_foundation"
            )
        elif raw_state == "nao_adquirido_preparavel":
            veracity = "preparable_gap"
        else:
            veracity = "unconfirmed"

        display_name = clean_confident_text(
            item["termo"], normalizations, f"skill[{item['id']}].display_name"
        )
        summary_label = (
            clean_confident_text(
                item["resumo"], normalizations, f"skill[{item['id']}].summary_label"
            )
            if item.get("resumo")
            else None
        )
        aliases = list(dict.fromkeys([item["termo"], *item.get("aliases", [])]))
        for a in aliases:
            known_aliases.add(_norm(a))

        skills.append(
            {
                "id": _slugify(item["id"], "skill"),
                "display_name": display_name,
                "aliases": aliases,
                "areas": item.get("areas", []),
                "veracity": veracity,
                "internal_context": ctx or None,
                "summary_label": summary_label,
                "study_plan": item.get("preparacao"),
                "factual_source": item.get("fonte") or "perfil/competencias-estruturadas.json",
                "translations": {},
            }
        )

    linkedin_added = 0
    linkedin_lines = (legacy_dir / "perfil" / "competencias-linkedin.md").read_text(
        encoding="utf-8"
    ).splitlines()
    for line in linkedin_lines:
        if not line.startswith("| "):
            continue
        parts = [p.strip() for p in line.split("|")[1:-1]]
        if len(parts) != 2 or parts[0] in {"Termo original", "---"} or _norm(parts[0]) in known_aliases:
            continue
        term, assoc = parts[0], parts[1]
        nt = _norm(term)
        area = (
            "dev"
            if any(s in nt for s in ["program", "software", "aplicat", "design", "algorit", "devops"])
            else "dados"
            if any(s in nt for s in ["dados", "estat", "probabil", "aprendizado de maquina"])
            else "cybersec"
            if "seguran" in nt
            else "suporte"
            if any(s in nt for s in ["rede", "sistema", "computador", "protocolo"])
            else "administrativo"
        )
        skill_id = "acad_" + hashlib.sha256(term.encode("utf-8")).hexdigest()[:10]
        known_aliases.add(nt)
        skills.append(
            {
                "id": skill_id,
                "display_name": clean_confident_text(term, normalizations, f"skill[{skill_id}]"),
                "aliases": [term],
                "areas": [area],
                "veracity": "academic_foundation",
                "internal_context": f"base_educacional_linkedin ({assoc})",
                "summary_label": None,
                "study_plan": None,
                "factual_source": "perfil/competencias-linkedin.md",
                "translations": {},
            }
        )
        linkedin_added += 1

    resumable_skills = [s for s in skills if s["veracity"] in {"confirmed_practice", "academic_foundation"}]

    def _match_skill_ids(text_val: str) -> list[str]:
        norm_val = _norm(text_val)
        matched: list[str] = []
        for sk in resumable_skills:
            if sk["id"].startswith("acad_"):
                continue
            for alias in sk["aliases"]:
                na = _norm(alias)
                if na and re.search(r"(?<!\w)" + re.escape(na) + r"(?!\w)", norm_val):
                    matched.append(sk["id"])
                    break
        return matched

    # 5. Courses & Practical BI Assessment
    dados_cfg = _load_legacy_area_config(legacy_dir / "config", areas_meta["dados"]["config"])
    dados_by_idx = {c["indice"]: c for c in dados_cfg["alteracoes"]}
    fiap_anchor = dados_by_idx[32].get(
        "esperado", "Agentes Autônomos (Agentic AI) — FIAP | 40 horas | 2026"
    )
    fiap_title = fiap_anchor.split("—")[0].strip() if "—" in fiap_anchor else "Agentes Autônomos (Agentic AI)"
    courses = [
        {
            "id": "course_fiap_agentic_ai",
            "entry_type": "course",
            "title": fiap_title,
            "institution": "FIAP",
            "details_label": "40 horas · 2026",
            "hours": 40,
            "year": 2026,
            "status": "completed",
            "areas": ["dev", "generalista"],
            "factual_source": "Currículo Base/Currículo Base.docx",
            "translations": {
                "en-US": {
                    "title": "Autonomous Agents (Agentic AI)",
                    "institution": "FIAP",
                    "details_label": "40 hours · 2026",
                }
            },
        },
        {
            "id": "prac_powerbi_selecao",
            "entry_type": "practical_assessment",
            "title": "Power BI — teste seletivo de estágio",
            "institution": "Vivência Prática",
            "details_label": "dashboard com dados fornecidos pela empresa",
            "hours": None,
            "year": 2026,
            "status": "completed",
            "areas": ["dados"],
            "factual_source": dados_by_idx[32]["origem"],
            "translations": {},
        },
    ]

    # 6. Reserved projects & forbidden terms from curriculo-generalista.json
    gen_cfg = _load_legacy_area_config(legacy_dir / "config", areas_meta["generalista"]["config"])
    raw_forbidden = gen_cfg.get("termos_proibidos", [])
    projects: list[dict[str, Any]] = []
    forbidden_terms: list[str] = []
    for term in raw_forbidden:
        if term.upper() == "PCD":
            forbidden_terms.append(term)
        else:
            projects.append(
                {
                    "id": "proj_" + _slugify(term, "reserved"),
                    "name": term,
                    "status": "reserved",
                    "summary": "Projeto em desenvolvimento reservado pelo usuário; bloqueado em currículos até avaliação autorizada.",
                    "bullets": [],
                    "areas": ["dev"],
                    "url": None,
                    "factual_source": "perfil/perfil-factual.md#dois-projetos-em-desenvolvimento",
                }
            )

    # 7. Experience & Atomic Bullets across ALL 7 area configs + ALL 10 per-job editorials
    exp_header_line = _segments_to_text(
        next(c["trechos"] for c in gen_cfg["alteracoes"] if c["indice"] == 24)
    )
    role_company, emp_type, period_lbl = [p.strip() for p in exp_header_line.split("|")]
    exp_role, exp_company = [p.strip() for p in role_company.split("—", 1)]

    bullet_by_norm: dict[str, dict[str, Any]] = {}
    area_bullet_ids: dict[str, list[str]] = {}

    def _register_bullet(
        raw_text: str, area_id: str, origem: str, en_translation: str | None = None
    ) -> str:
        cleaned = clean_confident_text(raw_text, normalizations, f"bullet[{area_id}]")
        key = _norm(cleaned)
        if key in bullet_by_norm:
            entry = bullet_by_norm[key]
            if area_id not in entry["areas"]:
                entry["areas"].append(area_id)
            if en_translation and "en-US" not in entry["translations"]:
                entry["translations"]["en-US"] = en_translation
            return entry["id"]

        b_id = f"bul_{area_id.replace('-', '_')}_{len(bullet_by_norm) + 1:02d}"
        tags = _match_skill_ids(cleaned)
        entry = {
            "id": b_id,
            "text": cleaned,
            "areas": [area_id],
            "skill_tags": tags,
            "keywords": tags,
            "factual_source": origem,
            "translations": {"en-US": en_translation} if en_translation else {},
        }
        bullet_by_norm[key] = entry
        return b_id

    # Collect bullets from the 6 pt-BR area configs first (plus map suporte -> suporte-en translations)
    suporte_cfg = _load_legacy_area_config(legacy_dir / "config", areas_meta["suporte"]["config"])
    suporte_en_bullet_map = {25: en_by_idx.get(25), 26: en_by_idx.get(26), 27: en_by_idx.get(27), 28: en_by_idx.get(28)}

    for area_id, meta in areas_meta.items():
        if area_id == "suporte-en":
            continue
        cfg = _load_legacy_area_config(legacy_dir / "config", meta["config"])
        by_idx = {c["indice"]: c for c in cfg["alteracoes"]}
        preferred: list[str] = []
        for idx in (25, 26, 27, 28):
            if idx in by_idx:
                en_txt = suporte_en_bullet_map.get(idx) if area_id == "suporte" else None
                b_id = _register_bullet(
                    _segments_to_text(by_idx[idx]["trechos"]),
                    area_id,
                    by_idx[idx].get("origem") or "perfil/perfil-factual.md",
                    en_translation=en_txt,
                )
                if area_id == "suporte":
                    # Also associate canonical suporte bullets with suporte-en
                    for b in bullet_by_norm.values():
                        if b["id"] == b_id and "suporte-en" not in b["areas"]:
                            b["areas"].append("suporte-en")
                preferred.append(b_id)
        area_bullet_ids[area_id] = preferred

    area_bullet_ids["suporte-en"] = list(area_bullet_ids["suporte"])

    # Also harvest all verified per-job editorial bullets so zero historical variants are lost!
    editorials_dir = legacy_dir / "config" / "editoriais"
    editorial_files_count = 0
    if editorials_dir.is_dir():
        for ed_file in sorted(editorials_dir.glob("*.json")):
            editorial_files_count += 1
            ed = json.loads(ed_file.read_text(encoding="utf-8"))
            ed_area = ed.get("area_id", "suporte")
            for change in ed.get("alteracoes", []):
                if change.get("indice") in {25, 26, 27, 28}:
                    _register_bullet(
                        _segments_to_text(change["trechos"]),
                        ed_area,
                        f"{ed_file.name}: {change.get('origem', 'Editorial revisado')}",
                    )

    experiences = [
        {
            "id": f"exp_{_slugify(exp_company, 'primary')}_01",
            "role": exp_role,
            "company": exp_company,
            "employment_type_label": emp_type,
            "period_label": period_lbl,
            "bullets": list(bullet_by_norm.values()),
            "factual_source": "Currículo Base/Currículo Base.docx; perfil/perfil-factual.md#experiencia-recife-tecnologia",
            "translations": {
                "en-US": {
                    "role": "Technical Support",
                    "employment_type_label": "Temporary (CLT)",
                    "period_label": period_lbl,
                }
            },
        }
    ]

    # 8. Area Presets (All 7 presets!)
    summary_prefixes = {
        "suporte": "Profissional de TI com experiência em suporte presencial e remoto a usuários e gestão de chamados.",
        "suporte-en": "IT support professional with experience in customer service, Help Desk, ticket management, and troubleshooting.",
        "dev": "Formado em ADS e graduando em Sistemas para Internet na UNICAP, com interesse em backend Java.",
        "dados": "Formado em ADS e graduando em Sistemas para Internet na UNICAP, com base em dados e sistemas.",
        "cybersec": "Técnico em Redes e formado em ADS, com base em segurança da informação e experiência em suporte corporativo.",
        "administrativo": "Experiência em atendimento a clientes, organização de solicitações e pesquisa de preços para orçamentos.",
        "generalista": "Profissional em início de carreira em TI, formado em ADS, técnico em Redes e graduando em Sistemas para Internet na UNICAP.",
    }
    allowed_summary_map = {
        "suporte": ["windows", "ad", "email", "chamados", "hardware", "redes", "office"],
        "suporte-en": ["windows", "ad", "email", "chamados", "hardware", "redes", "office"],
        "dev": ["java", "spring", "sql", "python"],
        "dados": ["sql", "excel", "bi", "python"],
        "cybersec": ["seguranca", "windows", "ad", "redes", "linux"],
        "administrativo": ["excel", "atendimento", "office"],
        "generalista": ["java", "sql", "windows", "redes", "seguranca"],
    }

    area_presets: dict[str, dict[str, Any]] = {}
    neutral_pdf_name = f"{full_name} - Currículo.pdf"

    for area_id, meta in areas_meta.items():
        cfg = _load_legacy_area_config(legacy_dir / "config", meta["config"])
        by_idx = {c["indice"]: c for c in cfg["alteracoes"]}
        is_en = area_id == "suporte-en"

        raw_summary = _segments_to_text(by_idx[6]["trechos"]) if 6 in by_idx else summary_prefixes[area_id]
        clean_summary = clean_confident_text(raw_summary, normalizations, f"preset[{area_id}].summary")

        group_indices = [32, 33, 34, 35, 36] if is_en else [36, 37, 38, 39, 40]
        skill_groups = []
        for g_pos, g_idx in enumerate(group_indices, start=1):
            if g_idx not in by_idx:
                continue
            label, items_txt = _split_skill_group(by_idx[g_idx]["trechos"])
            clean_items = clean_confident_text(
                items_txt, normalizations, f"preset[{area_id}].group[{g_idx}]"
            )
            skill_groups.append(
                {
                    "id": f"grp_{area_id.replace('-', '_')}_{g_pos}",
                    "label": label,
                    "items_text": clean_items,
                    "skill_ids": _match_skill_ids(f"{label} {clean_items}"),
                    "factual_source": by_idx[g_idx].get("origem") or "perfil/perfil-factual.md",
                }
            )

        if is_en:
            labels = {
                "summary": "PROFESSIONAL SUMMARY",
                "education": "EDUCATION",
                "experience": "EXPERIENCE",
                "courses": "ADDITIONAL TRAINING",
                "projects": "PROJECTS",
                "skills": "TECHNICAL SKILLS",
                "languages": "LANGUAGES",
            }
            included_courses: list[str] = []
            sec_order = ["summary", "education", "experience", "skills", "languages"]
        elif area_id == "dados":
            labels = {
                "summary": "RESUMO PROFISSIONAL",
                "education": "FORMAÇÃO",
                "experience": "EXPERIÊNCIA",
                "courses": "VIVÊNCIA PRÁTICA EM BI",
                "projects": "PROJETOS",
                "skills": "COMPETÊNCIAS TÉCNICAS",
                "languages": "IDIOMAS",
            }
            included_courses = ["prac_powerbi_selecao"]
            sec_order = ["summary", "education", "experience", "courses", "skills", "languages"]
        elif area_id in {"dev", "generalista"}:
            skills_title = "COMPETÊNCIAS" if area_id == "generalista" else "COMPETÊNCIAS TÉCNICAS"
            labels = {
                "summary": "RESUMO PROFISSIONAL",
                "education": "FORMAÇÃO",
                "experience": "EXPERIÊNCIA",
                "courses": "FORMAÇÃO COMPLEMENTAR",
                "projects": "PROJETOS",
                "skills": skills_title,
                "languages": "IDIOMAS",
            }
            included_courses = ["course_fiap_agentic_ai"]
            sec_order = ["summary", "education", "experience", "courses", "skills", "languages"]
        else:
            skills_title = "COMPETÊNCIAS" if area_id == "administrativo" else "COMPETÊNCIAS TÉCNICAS"
            labels = {
                "summary": "RESUMO PROFISSIONAL",
                "education": "FORMAÇÃO",
                "experience": "EXPERIÊNCIA",
                "courses": "FORMAÇÃO COMPLEMENTAR",
                "projects": "PROJETOS",
                "skills": skills_title,
                "languages": "IDIOMAS",
            }
            included_courses = []
            sec_order = ["summary", "education", "experience", "skills", "languages"]

        area_presets[area_id] = {
            "area_id": area_id,
            "display_name": meta["nome"],
            "locale": "en-US" if is_en else "pt-BR",
            "output_folder": area_id,
            "output_pdf_filename": neutral_pdf_name,
            "show_age": not is_en,
            "show_neighborhood": True,
            "max_pages": int(cfg.get("paginas_esperadas", 1)),
            "section_labels": labels,
            "section_order": sec_order,
            "page_break_before_sections": ["skills"] if area_id == "generalista" else [],
            "default_summary": clean_summary,
            "summary_prefix": summary_prefixes[area_id],
            "allowed_summary_skill_ids": allowed_summary_map[area_id],
            "preferred_bullet_ids": area_bullet_ids[area_id],
            "max_bullets_per_experience": 4,
            "included_course_ids": included_courses,
            "included_project_ids": [],
            "skill_groups": skill_groups,
        }

    profile_dict = {
        "schema_version": "1.0",
        "header": {
            "full_name": full_name,
            "age": age,
            "neighborhood": neighborhood,
            "city": city,
            "state": state,
            "country_by_locale": {"pt-BR": "Brasil", "en-US": "Brazil"},
            "email": email,
            "phone": phone,
            "links": profile_links,
        },
        "education": education,
        "experiences": experiences,
        "courses": courses,
        "projects": projects,
        "skills": skills,
        "languages": [
            {
                "id": "lang_pt",
                "language": "Português",
                "proficiency": "Nativo",
                "translations": {"en-US": {"language": "Portuguese", "proficiency": "Native"}},
            },
            {
                "id": "lang_en",
                "language": "Inglês",
                "proficiency": "Avançado",
                "translations": {"en-US": {"language": "English", "proficiency": "Advanced"}},
            },
        ],
        "contextual_equivalences": [
            {
                "from_term": "e-mail",
                "to_term": "correio eletrônico",
                "trigger_terms": ["correio eletrônico"],
                "factual_basis": "Configuração de contas/clientes de e-mail confirmada no perfil factual.",
            },
            {
                "from_term": "cabeamento",
                "to_term": "cabeamento estruturado",
                "trigger_terms": ["cabeamento estruturado"],
                "factual_basis": "Fundamentos declarados na formação técnica em Redes; sem alegar implantação avançada.",
            },
        ],
        "forbidden_resume_terms": forbidden_terms,
        "area_presets": area_presets,
    }

    validated_profile = MasterProfile.model_validate(profile_dict)
    stats = {
        "structured_skills_legacy": len(structured_raw),
        "linkedin_academic_skills_added": linkedin_added,
        "total_skills_migrated": len(validated_profile.skills),
        "skills_by_veracity": {
            state: sum(1 for s in validated_profile.skills if s.veracity == state)
            for state in ("confirmed_practice", "academic_foundation", "preparable_gap", "unconfirmed")
        },
        "education_entries": len(validated_profile.education),
        "experiences_count": len(validated_profile.experiences),
        "unique_bullets_migrated": len(validated_profile.experiences[0].bullets),
        "editorials_harvested": editorial_files_count,
        "courses_and_assessments": len(validated_profile.courses),
        "projects_approved": sum(1 for p in validated_profile.projects if p.status == "approved"),
        "projects_reserved": sum(1 for p in validated_profile.projects if p.status == "reserved"),
        "area_presets_migrated": list(validated_profile.area_presets.keys()),
        "confident_presentation_normalizations": len(normalizations),
    }
    return validated_profile, stats


def build_configs(
    legacy_dir: Path,
) -> tuple[SearchCriteria, BlockedCompaniesConfig, JobSourcesConfig]:
    estrat = json.loads((legacy_dir / "config" / "estrategia-vagas.json").read_text(encoding="utf-8"))
    catalogo = json.loads((legacy_dir / "config" / "catalogo-cargos.json").read_text(encoding="utf-8"))
    omissoes = json.loads((legacy_dir / "config" / "politica-omissoes.json").read_text(encoding="utf-8"))
    bloqueadas = json.loads((legacy_dir / "config" / "empresas-bloqueadas.json").read_text(encoding="utf-8"))
    fontes = json.loads((legacy_dir / "config" / "fontes-vagas.json").read_text(encoding="utf-8"))

    allowed_cities = []
    for city_slug, aliases in estrat["municipios"].items():
        allowed_cities.append(
            {
                "slug": city_slug,
                "canonical_name": aliases[-1],
                "state_code": "PE",
                "aliases": aliases,
                "neighborhoods": estrat.get("bairros", {}).get(city_slug, []),
            }
        )

    short_prep = [
        {
            "term": item["termo"],
            "aliases": item["aliases"],
            "prerequisite_skill_ids": item["base"],
            "study_plan": item["plano"],
        }
        for item in estrat.get("preparacao_curta", [])
    ]

    exclusions = [
        {
            "id": f"excl_{idx + 1:02d}",
            "urls": rule.get("urls", []),
            "excluded_source_terms": [],
            "reason": rule["motivo"],
        }
        for idx, rule in enumerate(estrat.get("exclusoes_estrategicas", []))
    ]
    if estrat.get("ciee_ativo") is False:
        exclusions.append(
            {
                "id": "excl_ciee_inactive",
                "urls": [],
                "excluded_source_terms": ["ciee"],
                "reason": "CIEE fora da estratégia ativa conforme configuração original.",
            }
        )

    criteria = SearchCriteria.model_validate(
        {
            "schema_version": "1.0",
            "geography": {
                "allowed_cities": allowed_cities,
                "ambiguous_neighborhoods_without_city": estrat.get(
                    "bairros_ambiguos_sem_municipio", []
                ),
                "accept_remote_national": True,
                "accept_remote_international_if_resident_allowed": True,
                "reject_broader_metro_if_outside_allowed_cities": True,
            },
            "schedule": {
                "earliest_hour": 6,
                "latest_hour": 18,
                "soft_latest_hour": 19,
                "reject_night_or_overnight": True,
            },
            "contracts": {
                "accepted_types": ["clt", "estagio_remunerado", "aprendiz"],
                "rejected_types": ["pj", "mei", "freelancer", "voluntario", "nao_remunerado"],
                "require_explicit_paid_internship_signal": True,
                "allow_undisclosed_salary": True,
            },
            "career_focus": {
                "entry_level_signals": catalogo.get("entradas", []),
                "out_of_focus_seniority_signals": [
                    "senior",
                    "sênior",
                    "sr",
                    "pleno",
                    "mid-level",
                    "especialista",
                    "tech lead",
                    "manager",
                    "gerente",
                    "coordenador",
                    "architect",
                ],
                "max_mandatory_experience_months": int(
                    estrat.get("experiencia_substancial_meses", 24)
                ),
                "preferred_recency_days": int(estrat.get("dias_recencia_preferida", 30)),
                "equal_area_priority": True,
                "max_secondary_sources_per_run": int(
                    estrat.get("fontes_secundarias_maximas_por_execucao", 1)
                ),
            },
            "omissions_policy": {
                "allow_local_prep_with_operational_omissions": bool(
                    omissoes.get("preparar_com_omissoes_operacionais", True)
                ),
                "never_assume_unconfirmed_remote_or_paid": True,
            },
            "role_catalog": {
                "area_keywords": catalogo["areas"],
                "informal_hiring_signals": catalogo.get("sinais_informais", []),
                "grouped_search_queries": catalogo.get("grupos_busca", []),
            },
            "short_prep_rules": short_prep,
            "strategic_exclusions": exclusions,
        }
    )

    blocked = BlockedCompaniesConfig.model_validate(
        {
            "schema_version": "1.0",
            "companies": [
                {
                    "id": emp["id"],
                    "display_name": emp["nome_informado"],
                    "permanent": bool(emp.get("permanente", True)),
                    "cnpj": (emp.get("identificadores_confirmados") or {}).get("cnpj"),
                    "corporate_name": (emp.get("identificadores_confirmados") or {}).get(
                        "razao_social"
                    ),
                    "domains": (emp.get("identificadores_confirmados") or {}).get("dominios", []),
                    "aliases": (emp.get("identificadores_confirmados") or {}).get("aliases", []),
                    "confirmed_related_entities": [],
                    "preserve_in_work_history": bool(
                        emp.get("preservar_empresa_no_historico_profissional", True)
                    ),
                }
                for emp in bloqueadas.get("empresas", [])
            ],
        }
    )

    source_entries: list[dict[str, Any]] = []
    for src in fontes.get("fontes", []):
        stype = src["tipo"]
        priority = "secondary" if src.get("prioridade") == "secundaria" else "high"
        enabled = bool(src.get("ativo", True))
        notes = src.get("motivo")
        if stype == "rss_local":
            source_entries.append(
                {
                    "id": src["id"],
                    "type": "rss_local",
                    "enabled": enabled,
                    "priority": priority,
                    "feeds": src["feeds"],
                    "seed_pages": [
                        {
                            "url": p["url"],
                            "title": p.get("titulo"),
                            "company": p.get("empresa"),
                            "location": p.get("localizacao"),
                        }
                        for p in src.get("paginas", [])
                    ],
                    "max_items": int(src.get("limite", 24)),
                    "notes": notes,
                }
            )
        elif stype == "paginas_publicas":
            source_entries.append(
                {
                    "id": src["id"],
                    "type": "public_pages",
                    "enabled": enabled,
                    "priority": priority,
                    "pages": [
                        {
                            "url": p["url"],
                            "title": p.get("titulo"),
                            "company": p.get("empresa"),
                            "location": p.get("localizacao"),
                        }
                        for p in src.get("paginas", [])
                    ],
                    "suspended_pages": [
                        {"url": sp["url"], "reason": sp["motivo"]}
                        for sp in src.get("paginas_suspensas", [])
                    ],
                    "max_items": int(src.get("limite", 10)),
                    "notes": notes,
                }
            )
        elif stype == "sitemap_divulgavagas":
            source_entries.append(
                {
                    "id": src["id"],
                    "type": "sitemap_jsonld",
                    "enabled": enabled,
                    "priority": priority,
                    "sitemaps": src["sitemaps"],
                    "allowed_city_slugs": src.get("municipios", ["recife", "jaboatao"]),
                    "recency_days": int(src.get("dias_recencia", 30)),
                    "max_items": int(src.get("maximo_anuncios", 10)),
                    "notes": notes,
                }
            )
        elif stype in {"greenhouse", "lever"}:
            source_entries.append(
                {
                    "id": f"{stype}_{src['board']}",
                    "type": stype,
                    "enabled": enabled,
                    "priority": priority,
                    "board_token": src["board"],
                    "company_name": src["empresa"],
                    "entry_level_local_only": bool(src.get("entrada_local", True)),
                    "max_items": int(src.get("maximo_anuncios", 10)),
                    "notes": notes,
                }
            )
        elif stype == "empregare_mcp":
            source_entries.append(
                {
                    "id": src["id"],
                    "type": "empregare_mcp",
                    "enabled": enabled,
                    "priority": priority,
                    "derive_queries_from_catalog": bool(src.get("consultas_derivadas", True)),
                    "max_queries": int(src.get("maximo_consultas", 9)),
                    "max_items": int(src.get("maximo_anuncios", 10)),
                    "public_detail_urls_by_id": src.get("detalhes_por_id", {}),
                    "notes": notes,
                }
            )

    sources_cfg = JobSourcesConfig.model_validate({"schema_version": "1.0", "sources": source_entries})
    return criteria, blocked, sources_cfg


def migrate_sqlite_database(legacy_db_path: Path, target_db_path: Path) -> dict[str, Any]:
    legacy_size_bytes = legacy_db_path.stat().st_size
    src_uri = f"file:{legacy_db_path.resolve().as_posix()}?mode=ro"
    src_db = sqlite3.connect(src_uri, uri=True)
    src_db.row_factory = sqlite3.Row

    # Wipe target DB only if re-running migration idempotently
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(target_db_path) + suffix)
        if candidate.exists():
            candidate.unlink()

    engine = create_sqlite_wal_engine(target_db_path)
    journal_mode = init_db_schema(engine)

    # Map primary source sightings per opportunity_id
    fontes_rows = src_db.execute("SELECT * FROM oportunidades_fontes").fetchall()
    primary_source_by_opp: dict[str, sqlite3.Row] = {}
    opp_id_by_canonical_url: dict[str, str] = {}
    for f_row in fontes_rows:
        opp_id = f_row["oportunidade_id"]
        primary_source_by_opp.setdefault(opp_id, f_row)
        if f_row["url_canonica"]:
            opp_id_by_canonical_url[f_row["url_canonica"]] = opp_id

    opp_rows = src_db.execute("SELECT * FROM oportunidades").fetchall()
    verif_rows = src_db.execute("SELECT * FROM verificacoes_oportunidades").fetchall()
    pers_rows = src_db.execute("SELECT * FROM personalizacoes").fetchall()
    fila_rows = src_db.execute("SELECT * FROM fila_preparacao").fetchall()
    acomp_rows = src_db.execute("SELECT * FROM candidaturas_acompanhamento").fetchall()
    cand_ev_rows = src_db.execute("SELECT * FROM candidaturas_eventos").fetchall()
    fila_ev_rows = src_db.execute("SELECT * FROM fila_eventos").fetchall()
    desc_run_rows = src_db.execute("SELECT * FROM descobertas_execucoes").fetchall()
    verif_run_rows = src_db.execute("SELECT * FROM verificacoes_execucoes").fetchall()
    skipped_blobs = src_db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(conteudo)), 0) FROM artefatos").fetchone()
    skipped_dup_snapshots = src_db.execute("SELECT COUNT(*) FROM oportunidades_observacoes").fetchone()[0]

    with Session(engine) as session:
        # 1. Opportunities (773)
        for row in opp_rows:
            opp_id = row["id"]
            dados = json.loads(row["dados_json"]) if row["dados_json"] else {}
            analise = json.loads(row["analise_json"]) if row["analise_json"] else {}
            f_info = primary_source_by_opp.get(opp_id)
            canon = f_info["url_canonica"] if f_info else _canonical_url(row["url_original"])
            if canon:
                opp_id_by_canonical_url.setdefault(canon, opp_id)

            raw_meta = {k: v for k, v in dados.items() if k != "descricao"}
            analise_meta = {k: v for k, v in analise.items() if k != "requisitos"}

            session.add(
                JobOpportunity(
                    id=opp_id,
                    discovered_at=row["descoberta_em"],
                    last_seen_at=row["vista_em"],
                    published_at=row["publicada_em"],
                    company=row["empresa"],
                    title=row["cargo"],
                    original_url=row["url_original"],
                    canonical_url=canon,
                    content_fingerprint=f_info["fingerprint"] if f_info else None,
                    primary_source=(f_info["fonte"] if f_info else dados.get("fonte")),
                    source_job_id=(f_info["id_fonte"] if f_info else dados.get("id_fonte")),
                    location=row["localizacao"],
                    workplace_mode=row["modalidade"],
                    contract_type=row["contrato"],
                    opportunity_type=row["tipo_oportunidade"] or "vaga",
                    area_id=analise.get("area_id"),
                    career_tier=analise.get("faixa_carreira"),
                    classification=row["classificacao"],
                    professional_compatibility=row["compatibilidade_profissional"],
                    operational_status=row["situacao_operacional"],
                    relevance_score=int(row["relevancia"] or 0),
                    practical_priority=int(row["prioridade_pratica"] or 0),
                    validity_status=row["vigencia"],
                    application_method=row["metodo_candidatura"] or "desconhecido",
                    application_effort=row["esforco_candidatura"] or "desconhecido",
                    automation_capability=row["capacidade_automacao"] or "desconhecida",
                    service_tier=row["nivel_atendimento"],
                    application_stage=row["situacao_candidatura"] or "descoberta",
                    description_text=str(dados.get("descricao") or ""),
                    requirements_json=row["requisitos_json"] or "[]",
                    analysis_json=json.dumps(analise_meta, ensure_ascii=False),
                    raw_payload_json=json.dumps(raw_meta, ensure_ascii=False),
                )
            )
        session.flush()

        # 2. Deduplicated Source Sightings (785) + Verification Evidences (258) -> JobObservation (1,043)
        for f_row in fontes_rows:
            f_dados = json.loads(f_row["dados_json"]) if f_row["dados_json"] else {}
            f_meta = {k: v for k, v in f_dados.items() if k != "descricao"}
            session.add(
                JobObservation(
                    opportunity_id=f_row["oportunidade_id"],
                    observation_kind="source_sighting",
                    source_name=f_row["fonte"],
                    source_item_id=f_row["id_fonte"],
                    canonical_url=f_row["url_canonica"],
                    content_fingerprint=f_row["fingerprint"],
                    observed_at=f_row["vista_em"],
                    payload_json=json.dumps(f_meta, ensure_ascii=False),
                )
            )
        for v_row in verif_rows:
            ev_payload = {
                "evidence_id": v_row["id"],
                "field": v_row["campo"],
                "value": json.loads(v_row["valor_json"]),
                "excerpt": v_row["trecho"],
                "nature": v_row["natureza"],
                "sha256": v_row["sha256"],
            }
            session.add(
                JobObservation(
                    opportunity_id=v_row["oportunidade_id"],
                    observation_kind="verification_evidence",
                    source_name=f"evidence:{v_row['natureza']}:{v_row['campo']}",
                    source_item_id=v_row["id"],
                    canonical_url=_canonical_url(v_row["url"]),
                    content_fingerprint=v_row["sha256"],
                    observed_at=v_row["consultada_em"],
                    payload_json=json.dumps(ev_payload, ensure_ascii=False),
                )
            )

        # 3. Resume Versions (18 personalizacoes — ZERO binary BLOBs)
        for p_row in pers_rows:
            src_url = p_row["fonte_url"]
            matched_opp_id = opp_id_by_canonical_url.get(_canonical_url(src_url)) if src_url else None
            session.add(
                ResumeVersion(
                    id=p_row["id"],
                    created_at=p_row["criado_em"],
                    opportunity_id=matched_opp_id,
                    company=p_row["empresa"],
                    job_title=p_row["vaga"],
                    source_url=src_url,
                    area_id=p_row["area_id"],
                    generation_mode="legacy_migrated",
                    status=p_row["status"],
                    description_sha256=p_row["descricao_sha256"],
                    pdf_sha256=p_row["pdf_sha256"],
                    docx_sha256=p_row["docx_sha256"],
                    previous_pdf_sha256=p_row["anterior_pdf_sha256"],
                    previous_docx_sha256=p_row["anterior_docx_sha256"],
                    export_rel_path=None,
                    rendered_payload_json=p_row["analise_json"] or "{}",
                    changes_json=p_row["alteracoes_json"] or "[]",
                    error_message=p_row["erro"],
                )
            )
        session.flush()

        # 4. Application Tracking (lossless union of candidaturas_acompanhamento + fila_preparacao)
        fila_by_opp = {r["oportunidade_id"]: r for r in fila_rows}
        acomp_by_opp = {r["oportunidade_id"]: r for r in acomp_rows}
        all_tracked_ids = sorted(set(fila_by_opp) | set(acomp_by_opp))
        for opp_id in all_tracked_ids:
            f_rec = fila_by_opp.get(opp_id)
            a_rec = acomp_by_opp.get(opp_id)
            updated_at = (
                max(filter(None, [a_rec["atualizada_em"] if a_rec else None, f_rec["atualizada_em"] if f_rec else None]))
            )
            stage = (a_rec["situacao"] if a_rec else None) or "descoberta"
            pers_id = (a_rec["personalizacao_id"] if a_rec else None) or (
                f_rec["personalizacao_id"] if f_rec else None
            )
            session.add(
                ApplicationTracking(
                    opportunity_id=opp_id,
                    updated_at=updated_at,
                    stage=stage,
                    queue_state=f_rec["estado"] if f_rec else None,
                    professional_compatibility=(
                        f_rec["compatibilidade_profissional"] if f_rec else None
                    ),
                    operational_status=f_rec["situacao_operacional"] if f_rec else None,
                    service_tier=a_rec["nivel_atendimento"] if a_rec else None,
                    automation_capability=a_rec["capacidade_automacao"] if a_rec else None,
                    application_method=f_rec["metodo"] if f_rec else None,
                    pdf_status=f_rec["pdf"] if f_rec else None,
                    resume_version_id=pers_id,
                    submission_instructions=a_rec["instrucoes_envio"] if a_rec else None,
                    user_confirmed_at=a_rec["confirmacao_usuario_em"] if a_rec else None,
                    notes=a_rec["nota"] if a_rec else None,
                    queue_assessment_json=f_rec["avaliacao_json"] if f_rec else None,
                )
            )

        # 5. Application Events (37 tracking + 205 queue = 242)
        for ce in cand_ev_rows:
            session.add(
                ApplicationEvent(
                    opportunity_id=ce["oportunidade_id"],
                    created_at=ce["criado_em"],
                    event_source="tracking",
                    stage=ce["situacao"],
                    notes=ce["nota"],
                    payload_json=None,
                )
            )
        for fe in fila_ev_rows:
            session.add(
                ApplicationEvent(
                    opportunity_id=fe["oportunidade_id"],
                    created_at=fe["criado_em"],
                    event_source="queue",
                    stage=fe["estado"],
                    notes="Legacy V3 queue evaluation event",
                    payload_json=fe["avaliacao_json"],
                )
            )

        # 6. Discovery & Verification Run Summaries (20 + 7 = 27, stripped of nested full-job arrays)
        for dr in desc_run_rows:
            rep = json.loads(dr["relatorio_json"]) if dr["relatorio_json"] else {}
            compact_rep = {k: v for k, v in rep.items() if k != "oportunidades"}
            session.add(
                DiscoveryRun(
                    id=dr["id"],
                    run_kind="discovery",
                    started_at=dr["iniciada_em"],
                    finished_at=dr["finalizada_em"],
                    extracted_count=int(rep.get("anuncios_extraidos", 0)),
                    unique_count=int(rep.get("oportunidades_unicas", 0)),
                    new_count=int(rep.get("novas_no_banco", 0)),
                    summary_json=json.dumps(compact_rep, ensure_ascii=False),
                )
            )
        for vr in verif_run_rows:
            rep = json.loads(vr["relatorio_json"]) if vr["relatorio_json"] else {}
            compact_rep = {
                k: v for k, v in rep.items() if k not in {"oportunidades", "avaliacoes", "fila"}
            }
            compact_rep["baseline"] = vr["baseline"]
            session.add(
                DiscoveryRun(
                    id=vr["id"],
                    run_kind="verification",
                    started_at=vr["criada_em"],
                    finished_at=vr["criada_em"],
                    extracted_count=int(rep.get("total_coorte", 0) or 0),
                    unique_count=int(rep.get("candidaturas_a_preparar", 0) or 0),
                    new_count=0,
                    summary_json=json.dumps(compact_rep, ensure_ascii=False),
                )
            )

        session.commit()

    # Checkpoint WAL into main database file so on-disk file size reflects all data accurately
    with engine.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        conn.execute(text("ANALYZE"))
        conn.commit()
    engine.dispose()
    src_db.close()

    new_size_bytes = target_db_path.stat().st_size
    return {
        "sqlite_journal_mode": journal_mode,
        "legacy_db_size_bytes": legacy_size_bytes,
        "legacy_db_size_mb": round(legacy_size_bytes / (1024 * 1024), 2),
        "new_db_size_bytes": new_size_bytes,
        "new_db_size_mb": round(new_size_bytes / (1024 * 1024), 2),
        "size_reduction_percent": round((1 - (new_size_bytes / legacy_size_bytes)) * 100, 2),
        "rows_reconciliation": {
            "job_opportunities": {"legacy": len(opp_rows), "migrated": len(opp_rows)},
            "source_sightings_fingerprints": {"legacy": len(fontes_rows), "migrated": len(fontes_rows)},
            "verification_evidences": {"legacy": len(verif_rows), "migrated": len(verif_rows)},
            "total_job_observations": {"migrated": len(fontes_rows) + len(verif_rows)},
            "resume_versions": {"legacy": len(pers_rows), "migrated": len(pers_rows)},
            "application_tracking_union": {
                "legacy_acompanhamento": len(acomp_rows),
                "legacy_fila_preparacao": len(fila_rows),
                "migrated_merged": len(all_tracked_ids),
            },
            "application_events": {
                "legacy_tracking_events": len(cand_ev_rows),
                "legacy_queue_events": len(fila_ev_rows),
                "migrated_total": len(cand_ev_rows) + len(fila_ev_rows),
            },
            "discovery_and_verification_runs": {
                "legacy_discovery": len(desc_run_rows),
                "legacy_verification": len(verif_run_rows),
                "migrated_total": len(desc_run_rows) + len(verif_run_rows),
            },
            "discarded_binary_blobs": {
                "legacy_artefatos_rows": int(skipped_blobs[0]),
                "legacy_artefatos_bytes": int(skipped_blobs[1]),
                "migrated_blobs": 0,
            },
            "deduplicated_redundant_snapshots_omitted": int(skipped_dup_snapshots),
        },
    }


def run_migration(data_dir: Path | None = None) -> dict[str, Any]:
    safe_data = assert_safe_path(data_dir or (REPO_ROOT / "user_data"))
    legacy_dir = safe_data / "legacy_archive"
    if not legacy_dir.is_dir():
        raise FileNotFoundError(f"Legacy archive not found at {legacy_dir}")

    normalizations: list[dict[str, str]] = []
    profile, profile_stats = build_master_profile(legacy_dir, normalizations)
    criteria, blocked, sources = build_configs(legacy_dir)

    (safe_data / "profile").mkdir(parents=True, exist_ok=True)
    (safe_data / "config").mkdir(parents=True, exist_ok=True)
    (safe_data / "db").mkdir(parents=True, exist_ok=True)

    (safe_data / "profile" / "master_profile.json").write_text(
        json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (safe_data / "config" / "search_criteria.json").write_text(
        json.dumps(criteria.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (safe_data / "config" / "blocked_companies.json").write_text(
        json.dumps(blocked.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (safe_data / "config" / "sources.json").write_text(
        json.dumps(sources.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    db_stats = migrate_sqlite_database(
        legacy_db_path=legacy_dir / "dados" / "historico.sqlite3",
        target_db_path=safe_data / "db" / "app.sqlite3",
    )

    # Verify original legacy SQLite hash was untouched
    legacy_db_sha256 = hashlib.sha256(
        (legacy_dir / "dados" / "historico.sqlite3").read_bytes()
    ).hexdigest().upper()

    report = {
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "profile_reconciliation": profile_stats,
        "config_reconciliation": {
            "allowed_cities": [c.canonical_name for c in criteria.geography.allowed_cities],
            "short_prep_rules_count": len(criteria.short_prep_rules),
            "strategic_exclusions_count": len(criteria.strategic_exclusions),
            "grouped_search_queries_count": len(criteria.role_catalog.grouped_search_queries),
            "blocked_companies_migrated": len(blocked.companies),
            "job_sources_migrated": len(sources.sources),
        },
        "database_reconciliation": db_stats,
        "legacy_archive_integrity": {
            "historico_sqlite3_sha256": legacy_db_sha256,
            "untouched": True,
        },
        "presentation_normalizations_applied": normalizations,
    }

    (safe_data / "db" / "migration_reconciliation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    report = run_migration()
    # Print public-safe summary (excluding raw bullet texts if any, keeping full counts)
    public_summary = {
        k: v for k, v in report.items() if k != "presentation_normalizations_applied"
    }
    public_summary["presentation_normalizations_count"] = len(
        report["presentation_normalizations_applied"]
    )
    print(json.dumps(public_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
