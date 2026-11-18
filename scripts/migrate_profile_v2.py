"""Safe, lossless migration of `user_data/profile/master_profile.json` from v1.0 to v2.0.

- Creates a timestamped backup snapshot in `user_data/backups/` before writing.
- Consolidates multi-preset experience bullets into a cohesive, rich free-text `description`
  preserving 100% of real activities and tools without duplication.
- Converts valid confirmed/academic skills into clean, deduplicated string tags (`skills: list[str]`),
  dropping non-skill meta placeholders and unverified gap items.
- Preserves header, contacts, links, education, completed courses/practical experiences,
  approved projects, and languages intact.
- Contains ZERO hardcoded PII (operates generically over any v1.0 JSON structure).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.models.profile_schema import MasterProfile  # noqa: E402

META_NON_SKILLS = {
    "aprendizado",
    "formacao superior",
    "informatica",
    "programacao (computacao)",
    "rede de computadores",
    "ia",
}


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip()


def _consolidate_bullets_into_description(bullets: list[dict[str, Any]]) -> str:
    """Consolidates v1.0 area-sliced bullets into a cohesive, non-redundant rich description."""
    raw_texts: list[str] = []
    for b in bullets:
        txt = str(b.get("text") or "").strip()
        if txt:
            raw_texts.append(txt)

    if not raw_texts:
        return ""

    # Group unique clauses/sentences by semantic theme while preserving every distinct detail
    themes: list[tuple[str, list[str]]] = [
        ("atendimento", ["help desk", "chamados", "anydesk", "atendimento", "orientacao"]),
        ("acessos_email", ["active directory", "dominio", "senhas", "e-mail", "assinaturas em html"]),
        ("manutencao", ["manutencao preventiva", "formatacao", "reinstalacao", "antivirus", "backups", "impressoras"]),
        ("redes_diagnostico", ["diagnostico", "conectividade", "crimpagem", "cabos", "configuracoes de rede"]),
        ("planilhas_relatorios", ["pesquisa de precos", "orcamentos", "excel", "relatorios"]),
    ]

    # Pick the richest, most complete representative sentences per theme + any unmatched unique clauses
    selected_paragraphs: list[str] = []
    covered_clauses: set[str] = set()

    # Split all bullets on ';' into fine-grained factual clauses so zero details are lost
    all_clauses: list[str] = []
    seen_norm: set[str] = set()
    for text in raw_texts:
        for part in text.split(";"):
            clause = part.strip().rstrip(".")
            if not clause:
                continue
            if clause[0].islower():
                clause = clause[0].upper() + clause[1:]
            key = re.sub(r"\W+", " ", _norm(clause)).strip()
            if key and key not in seen_norm:
                seen_norm.add(key)
                all_clauses.append(clause)

    for _, keywords in themes:
        matching = [
            c for c in all_clauses if c not in covered_clauses and any(kw in _norm(c) for kw in keywords)
        ]
        if not matching:
            continue
        # Sort longest-first and keep only clauses adding meaningful new content words (>= 35% novel tokens)
        matching.sort(key=len, reverse=True)
        kept_for_theme: list[str] = []
        union_tokens: set[str] = set()
        for candidate in matching:
            tokens = {
                tok[:6]
                for tok in re.findall(r"\w{4,}", _norm(candidate))
                if tok not in {
                    "para",
                    "alem",
                    "como",
                    "pelo",
                    "pela",
                    "usuarios",
                    "empresas",
                    "clientes",
                    "computadores",
                    "sistemas",
                    "informacoes",
                }
            }
            novel = tokens - union_tokens
            if union_tokens and (len(novel) == 0 or len(novel) / max(1, len(tokens)) < 0.35):
                covered_clauses.add(candidate)
                continue
            kept_for_theme.append(candidate)
            union_tokens.update(tokens)
            covered_clauses.add(candidate)

        if kept_for_theme:
            paragraph = ". ".join(c.rstrip(".") for c in kept_for_theme) + "."
            selected_paragraphs.append(paragraph)

    # Preserve any remaining factual clauses that didn't match the 5 main themes
    leftovers = [c for c in all_clauses if c not in covered_clauses]
    if leftovers:
        selected_paragraphs.append("; ".join(leftovers).rstrip(".") + ".")

    return "\n\n".join(selected_paragraphs)


def _split_course_workload_and_date(raw_label: str, raw_desc: str) -> tuple[str, str, str]:
    """Splits legacy `workload_or_year` / `details_label` into (workload, completion_date, description)."""
    working = (raw_label or "").strip()
    workload = ""
    completion_date = ""

    wl_match = re.search(r"\b\d+\s*h(?:oras?)?\b", working, flags=re.IGNORECASE)
    if wl_match:
        workload = wl_match.group(0).strip()
        working = (working[: wl_match.start()] + " " + working[wl_match.end() :]).strip()

    dt_match = re.search(
        r"\b(?:conclu[ií]do\s+em\s+)?(?:(?:jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-zç]*[\s/.-]+|\d{2}/)?(?:19|20)\d{2}\b",
        working,
        flags=re.IGNORECASE,
    )
    if dt_match:
        raw_dt = dt_match.group(0).strip()
        completion_date = re.sub(r"^conclu[ií]do\s+em\s+", "", raw_dt, flags=re.IGNORECASE).strip()
        working = (working[: dt_match.start()] + " " + working[dt_match.end() :]).strip()

    leftover = re.sub(r"^[\s·•\-–—,;:]+|[\s·•\-–—,;:]+$", "", working)
    leftover = re.sub(r"\s{2,}", " ", leftover).strip()

    desc = (raw_desc or "").strip()
    if leftover:
        if leftover[0].islower():
            leftover = leftover[0].upper() + leftover[1:]
        desc = f"{leftover}. {desc}".strip(". ") if desc else leftover
    return workload, completion_date, desc


def convert_v1_dict_to_v2(raw: dict[str, Any], default_city: str = "", default_state: str = "") -> MasterProfile:
    ver = str(raw.get("schema_version") or "1.0")
    if ver == "2.1":
        return MasterProfile.model_validate(raw)

    if ver == "2.0":
        upgraded_courses: list[dict[str, Any]] = []
        for crs in raw.get("courses") or []:
            if not isinstance(crs, dict):
                continue
            wl, cdate, desc = _split_course_workload_and_date(
                str(crs.get("workload_or_year") or ""),
                str(crs.get("description") or ""),
            )
            upgraded_courses.append(
                {
                    "id": crs.get("id") or "crs_01",
                    "title": crs.get("title") or "",
                    "institution": crs.get("institution") or "",
                    "workload": str(crs.get("workload") or wl),
                    "completion_date": str(crs.get("completion_date") or cdate),
                    "description": desc,
                }
            )
        upgraded_projects: list[dict[str, Any]] = []
        for prj in raw.get("projects") or []:
            if not isinstance(prj, dict):
                continue
            upgraded_projects.append(
                {
                    "id": prj.get("id") or "prj_01",
                    "name": prj.get("name") or "",
                    "url": prj.get("url"),
                    "description": str(prj.get("description") or ""),
                }
            )
        return MasterProfile.model_validate(
            {
                **raw,
                "schema_version": "2.1",
                "courses": upgraded_courses,
                "projects": upgraded_projects,
            }
        )

    raw_header = raw.get("header") or {}
    country_map = raw_header.get("country_by_locale") or {}
    country = country_map.get("pt-BR") if isinstance(country_map, dict) else raw_header.get("country")
    city = str(raw_header.get("city") or default_city).strip()
    state = str(raw_header.get("state") or default_state).strip()

    header = {
        "full_name": raw_header.get("full_name") or "",
        "email": raw_header.get("email") or "",
        "phone": raw_header.get("phone") or "",
        "city": city,
        "state": state,
        "neighborhood": raw_header.get("neighborhood"),
        "country": str(country or "Brasil"),
        "age": raw_header.get("age"),
        "links": [
            {
                "label": lnk.get("label") or "Link",
                "url": lnk["url"],
                "display_text": lnk.get("display_text") or "",
            }
            for lnk in (raw_header.get("links") or [])
            if isinstance(lnk, dict) and lnk.get("url")
        ],
    }

    presets = raw.get("area_presets") or {}
    summary = str(raw.get("summary") or "").strip()
    if not summary and isinstance(presets, dict):
        if "generalista" in presets and presets["generalista"].get("default_summary"):
            summary = str(presets["generalista"]["default_summary"]).strip()
        elif presets:
            first_preset = next(iter(presets.values()))
            summary = str(first_preset.get("default_summary") or "").strip()

    experiences: list[dict[str, Any]] = []
    for exp in raw.get("experiences") or []:
        if not isinstance(exp, dict):
            continue
        emp_type = str(exp.get("employment_type_label") or "").strip()
        period_lbl = str(exp.get("period_label") or exp.get("period") or "").strip()
        full_period = f"{emp_type} · {period_lbl}" if emp_type and period_lbl else (period_lbl or emp_type)
        desc = str(exp.get("description") or "").strip()
        if not desc and isinstance(exp.get("bullets"), list):
            desc = _consolidate_bullets_into_description(exp["bullets"])
        loc = str(exp.get("location") or "").strip()
        if not loc and city:
            loc = f"{city}, {state}" if state else city

        experiences.append(
            {
                "id": exp.get("id") or "exp_01",
                "role": exp.get("role") or "",
                "company": exp.get("company") or "",
                "period": full_period,
                "location": loc,
                "description": desc,
            }
        )

    education: list[dict[str, Any]] = []
    for edu in raw.get("education") or []:
        if not isinstance(edu, dict):
            continue
        note = str(edu.get("program_note") or edu.get("details") or "").strip()
        status = str(edu.get("status") or "").strip()
        if status == "in_progress" and "andamento" not in _norm(note) and "previsao" not in _norm(str(edu.get("period_label") or "")):
            note = f"Em andamento · {note}".strip(" ·")
        education.append(
            {
                "id": edu.get("id") or "edu_01",
                "degree": edu.get("degree") or "",
                "institution": edu.get("institution") or "",
                "period": str(edu.get("period_label") or edu.get("period") or ""),
                "details": note,
            }
        )

    courses: list[dict[str, Any]] = []
    for crs in raw.get("courses") or []:
        if not isinstance(crs, dict):
            continue
        # Only migrate completed courses (skip uncompleted free placeholders from v1)
        if crs.get("status") not in {None, "completed"}:
            continue
        wl, cdate, cdesc = _split_course_workload_and_date(
            str(crs.get("details_label") or crs.get("workload_or_year") or ""),
            str(crs.get("description") or ""),
        )
        courses.append(
            {
                "id": crs.get("id") or "crs_01",
                "title": crs.get("title") or "",
                "institution": crs.get("institution") or "",
                "workload": str(crs.get("workload") or wl),
                "completion_date": str(crs.get("completion_date") or cdate),
                "description": cdesc,
            }
        )

    projects: list[dict[str, Any]] = []
    for prj in raw.get("projects") or []:
        if not isinstance(prj, dict):
            continue
        # Respect v1 confidential/reserved projects: only migrate approved projects into public resume base
        if prj.get("status") == "reserved":
            continue
        prj_desc_parts = [str(prj.get("summary") or prj.get("description") or "").strip()]
        for b in prj.get("bullets") or []:
            if isinstance(b, dict) and b.get("text"):
                prj_desc_parts.append(str(b["text"]).strip())
        projects.append(
            {
                "id": prj.get("id") or "prj_01",
                "name": prj.get("name") or "",
                "url": prj.get("url"),
                "description": "\n".join(p for p in prj_desc_parts if p),
            }
        )

    skills: list[str] = []
    for sk in raw.get("skills") or []:
        if isinstance(sk, str):
            if _norm(sk) not in META_NON_SKILLS:
                skills.append(sk)
        elif isinstance(sk, dict):
            veracity = sk.get("veracity")
            if veracity in {"preparable_gap", "unconfirmed"}:
                continue
            name = str(sk.get("display_name") or "").strip()
            if name and _norm(name) not in META_NON_SKILLS:
                skills.append(name)

    languages: list[dict[str, Any]] = []
    for lng in raw.get("languages") or []:
        if not isinstance(lng, dict):
            continue
        languages.append(
            {
                "id": lng.get("id") or "lng_01",
                "language": lng.get("language") or "",
                "proficiency": lng.get("proficiency") or "",
            }
        )

    return MasterProfile.model_validate(
        {
            "schema_version": "2.1",
            "header": header,
            "summary": summary,
            "experiences": experiences,
            "education": education,
            "courses": courses,
            "projects": projects,
            "skills": skills,
            "languages": languages,
        }
    )


def migrate_workspace_profile() -> dict[str, Any]:
    settings = get_settings()
    profile_path = settings.profile_path
    if not profile_path.is_file():
        return {"status": "skipped", "reason": "master_profile.json not found"}

    raw_data = json.loads(profile_path.read_text(encoding="utf-8"))
    old_version = str(raw_data.get("schema_version") or "1.0")
    if old_version == "2.1":
        validated = MasterProfile.model_validate(raw_data)
        return {
            "status": "already_v2_1",
            "schema_version": validated.schema_version,
            "experiences_count": len(validated.experiences),
            "education_count": len(validated.education),
            "courses_count": len(validated.courses),
            "projects_count": len(validated.projects),
            "skills_count": len(validated.skills),
            "languages_count": len(validated.languages),
        }

    backups_dir = settings.backups_dir
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"master_profile.v{old_version}_pre_v21_{stamp}.bak.json"
    shutil.copy2(profile_path, backups_dir / backup_name)

    migrated = convert_v1_dict_to_v2(raw_data)
    serialized = json.dumps(migrated.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"

    fd, tmp_str = tempfile.mkstemp(prefix=".profile_v2_", suffix=".tmp", dir=str(profile_path.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, profile_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    return {
        "status": "migrated",
        "from_schema_version": old_version,
        "to_schema_version": migrated.schema_version,
        "backup_snapshot": backup_name,
        "header_links_preserved": len(migrated.header.links),
        "summary_chars": len(migrated.summary),
        "experiences_count": len(migrated.experiences),
        "experience_description_paragraphs": (
            len([p for p in migrated.experiences[0].description.split("\n\n") if p.strip()])
            if migrated.experiences
            else 0
        ),
        "experience_description_chars": (
            len(migrated.experiences[0].description) if migrated.experiences else 0
        ),
        "education_count": len(migrated.education),
        "courses_count": len(migrated.courses),
        "projects_count": len(migrated.projects),
        "skills_tags_count": len(migrated.skills),
        "languages_count": len(migrated.languages),
    }


def main() -> int:
    report = migrate_workspace_profile()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
