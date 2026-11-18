"""Resume file upload & smart block-grouping parser (`POST /api/profile/import-file` for `.pdf` and `.docx`).

1. Extracts linear text (preserving visual paragraph/block breaks) + embedded HTTPS links from `.pdf`
   (`pypdf`) or `.docx` (Word XML paragraphs, tables, headers, and hyperlink relationships).
2. When Google Gemini AI is connected (`settings.ai_connected`), masks sensitive PII (`email`, `phone`, `urls`),
   structures the resume into `MasterProfile` v2.1 via `GeminiAdapter` with explicit block-grouping instructions,
   and rehydrates PII locally.
3. Uses a smart semantic block-grouping parser (`parse_education_blocks`, `parse_experience_blocks`,
   `parse_course_blocks`) both as a deterministic offline fallback and as a structural safety net.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from pypdf import PdfReader

from app.adapters.llm.pii_stripper import (
    OUTBOUND_EMAIL_RE,
    OUTBOUND_PHONE_RE,
    OUTBOUND_URL_RE,
    rehydrate_text,
    strip_pii_for_profile_import,
)
from app.adapters.llm.providers import LlmAdapterError, build_llm_adapter
from app.core.config import AppSettings
from app.models.profile_schema import (
    CandidateHeader,
    CourseEntry,
    EducationEntry,
    ExperienceEntry,
    LanguageEntry,
    MasterProfile,
    ProfileLink,
    ProjectEntry,
)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class ProfileImportError(ValueError):
    """Raised when an uploaded file is unsupported, corrupted, or empty."""


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _norm_heading(line: str) -> str:
    stripped = _strip_accents(line)
    return re.sub(r"[^a-z0-9\s/]", "", stripped).strip()


def _normalize_https_url(raw_url: str) -> str | None:
    cleaned = raw_url.strip().rstrip(".,;)>]")
    if not cleaned:
        return None
    if cleaned.lower().startswith("http://"):
        cleaned = "https://" + cleaned[7:]
    elif not cleaned.lower().startswith("https://"):
        if cleaned.lower().startswith(("www.", "github.com/", "linkedin.com/", "gitlab.com/")):
            cleaned = "https://" + cleaned
        else:
            return None
    if " " in cleaned or "." not in cleaned:
        return None
    return cleaned[:300]


def _infer_link_label(url: str) -> str:
    low = url.lower()
    if "github.com" in low:
        return "GitHub"
    if "linkedin.com" in low:
        return "LinkedIn"
    if "gitlab.com" in low:
        return "GitLab"
    return "Portfólio"


def extract_text_and_links_from_pdf(raw_bytes: bytes) -> tuple[str, list[str]]:
    try:
        reader = PdfReader(BytesIO(raw_bytes))
    except Exception as exc:
        raise ProfileImportError("Não foi possível ler o arquivo PDF enviado.") from exc

    pages_text: list[str] = []
    uris: list[str] = []
    for page in reader.pages:
        txt = (page.extract_text() or "").strip()
        if txt:
            pages_text.append(txt)
        annots = page.get("/Annots")
        annot_list = annots.get_object() if hasattr(annots, "get_object") else annots
        if isinstance(annot_list, list):
            for ref in annot_list:
                annot = ref.get_object() if hasattr(ref, "get_object") else ref
                if isinstance(annot, dict) and annot.get("/Subtype") == "/Link":
                    action = annot.get("/A")
                    act_obj = action.get_object() if hasattr(action, "get_object") else action
                    if isinstance(act_obj, dict) and act_obj.get("/URI"):
                        norm_u = _normalize_https_url(str(act_obj["/URI"]))
                        if norm_u:
                            uris.append(norm_u)

    joined = "\n\n".join(pages_text).strip()
    return joined, list(dict.fromkeys(uris))


def _extract_paragraph_text(p_elem: ET.Element) -> str:
    parts: list[str] = []
    for node in p_elem.iter():
        if node.tag == f"{WORD_NS}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{WORD_NS}tab":
            parts.append(" ")
        elif node.tag == f"{WORD_NS}br":
            parts.append("\n")
    return "".join(parts).strip()


def _extract_docx_xml_blocks(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    body = root.find(f"{WORD_NS}body")
    container = body if body is not None else root

    lines: list[str] = []
    for child in list(container):
        if child.tag == f"{WORD_NS}p":
            txt = _extract_paragraph_text(child)
            if txt:
                lines.append(txt)
            elif lines and lines[-1] != "":
                lines.append("")
        elif child.tag == f"{WORD_NS}tbl":
            for row in child.findall(f".//{WORD_NS}tr"):
                cell_texts: list[str] = []
                for cell in row.findall(f"./{WORD_NS}tc"):
                    c_lines = [
                        _extract_paragraph_text(p)
                        for p in cell.findall(f".//{WORD_NS}p")
                    ]
                    c_joined = " ".join(x for x in c_lines if x).strip()
                    if c_joined:
                        cell_texts.append(c_joined)
                if cell_texts:
                    lines.append(" | ".join(dict.fromkeys(cell_texts)))
    return lines


def extract_text_and_links_from_docx(raw_bytes: bytes) -> tuple[str, list[str]]:
    try:
        zf = zipfile.ZipFile(BytesIO(raw_bytes))
    except Exception as exc:
        raise ProfileImportError("Arquivo .docx inválido ou corrompido.") from exc

    with zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise ProfileImportError("Estrutura interna Word XML não encontrada no .docx.")

        blocks: list[str] = []
        for hdr_name in sorted(n for n in names if n.startswith("word/header") and n.endswith(".xml")):
            blocks.extend(_extract_docx_xml_blocks(zf.read(hdr_name)))

        blocks.extend(_extract_docx_xml_blocks(zf.read("word/document.xml")))

        uris: list[str] = []
        for rel_name in sorted(n for n in names if n.startswith("word/_rels/") and n.endswith(".rels")):
            try:
                rel_root = ET.fromstring(zf.read(rel_name))
                for rel in rel_root.findall(f"{REL_NS}Relationship"):
                    target = rel.attrib.get("Target", "")
                    if "hyperlink" in rel.attrib.get("Type", "").lower():
                        norm_u = _normalize_https_url(target)
                        if norm_u:
                            uris.append(norm_u)
            except ET.ParseError:
                continue

    return "\n".join(blocks).strip(), list(dict.fromkeys(uris))


def _extract_contacts_from_raw_text(raw_text: str, embedded_urls: list[str]) -> dict[str, Any]:
    email_match = OUTBOUND_EMAIL_RE.search(raw_text)
    phone_match = OUTBOUND_PHONE_RE.search(raw_text)

    text_urls = [_normalize_https_url(m.group(0)) for m in OUTBOUND_URL_RE.finditer(raw_text)]
    all_urls: list[str] = []
    seen_u: set[str] = set()
    for candidate in [*embedded_urls, *(u for u in text_urls if u)]:
        if candidate and candidate.lower() not in seen_u and "mailto:" not in candidate.lower():
            seen_u.add(candidate.lower())
            all_urls.append(candidate)

    links = [
        ProfileLink(label=_infer_link_label(u), url=u)
        for u in all_urls[:6]
    ]
    return {
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(0).strip() if phone_match else "",
        "links": links,
    }


SECTION_HEADING_MAP: dict[str, set[str]] = {
    "summary": {
        "resumo",
        "resumo profissional",
        "sobre",
        "sobre mim",
        "objetivo",
        "objetivo profissional",
        "perfil profissional",
        "sintese de qualificacoes",
        "professional summary",
        "summary",
        "profile",
        "about",
    },
    "experience": {
        "experiencia",
        "experiencia profissional",
        "experiencias",
        "experiencias profissionais",
        "historico profissional",
        "atuacao profissional",
        "trajetoria profissional",
        "experience",
        "professional experience",
        "work experience",
    },
    "education": {
        "formacao",
        "formacao academica",
        "educacao",
        "escolaridade",
        "historico academico",
        "education",
        "academic background",
    },
    "courses": {
        "cursos",
        "formacao complementar",
        "certificacoes",
        "cursos e certificacoes",
        "cursos / certificacoes",
        "cursos complementares",
        "qualificacoes complementares",
        "aperfeicoamento",
        "additional training",
        "certifications",
        "courses",
    },
    "projects": {
        "projetos",
        "projetos relevantes",
        "projetos academicos",
        "projetos pessoais",
        "portfolio de projetos",
        "projects",
        "personal projects",
    },
    "skills": {
        "competencias",
        "competencias tecnicas",
        "habilidades",
        "habilidades tecnicas",
        "conhecimentos tecnicos",
        "tecnologias",
        "ferramentas e tecnologias",
        "skills",
        "technical skills",
    },
    "languages": {
        "idiomas",
        "linguas",
        "languages",
    },
}

DEGREE_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"graduacao|bacharelado|tecnologo|superior\s+de\s+tecnologia|licenciatura|"
    r"curso\s+tecnico|tecnico\s+em|ensino\s+medio|ensino\s+tecnico|"
    r"pos[\s-]?graduacao|especializacao|mestrado|doutorado|mba"
    r")\b",
    re.IGNORECASE,
)

INSTITUTION_ACRONYM_RE = re.compile(
    r"\b(?:"
    r"UNI[A-Z0-9]{2,}|UF[A-Z]{1,4}|UFR[A-Z]{1,3}|UE[A-Z]{2,4}|IF[A-Z]{2,4}|"
    r"ETE[A-Z0-9]*|ETEC|FATEC|CEFET|UPE|USP|PUC(?:-?[A-Z]{2})?|"
    r"SENAI|SENAC|FIAP|FGV|IBMEC|INSPER|IME|ITA"
    r")\b"
)

INSTITUTION_WORD_RE = re.compile(
    r"\b(?:"
    r"universidade|centro\s+universitario|faculdade|instituto\s+federal|"
    r"instituto|escola\s+tecnica|colegio|fundacao|cesar\s+school|"
    r"mackenzie|estacio|anhanguera|pitagoras|unopar|unicesumar|uninassau|unicap|uninter|etemac"
    r")\b",
    re.IGNORECASE,
)

MONTH_TOKEN = r"(?:jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-zç]*"

PERIOD_LINE_RE = re.compile(
    rf"(?:"
    rf"\binicio\s*:|\bprevisao\s+de\s+conclusao|\bconcluido\s+em\b|\bconclusao\s*:|"
    rf"\b(?:{MONTH_TOKEN}|\d{{2}})\s*/\s*(?:19|20)\d{{2}}\b|"
    rf"\b(?:19|20)\d{{2}}\s*[-–—a]\s*(?:(?:19|20)\d{{2}}|atual|presente|andamento)\b|"
    rf"^(?:em\s+andamento|cursando)$|"
    rf"^(?:19|20)\d{{2}}$"
    rf")",
    re.IGNORECASE,
)

DATE_RANGE_RE = re.compile(
    rf"(?:"
    rf"(?:{MONTH_TOKEN}|\d{{2}})\s*[/.\s]\s*(?:19|20)\d{{2}}\s*[-–—a]\s*(?:(?:{MONTH_TOKEN}|\d{{2}})\s*[/.\s]\s*(?:19|20)\d{{2}}|atual|presente|momento)|"
    rf"\b(?:19|20)\d{{2}}\s*[-–—]\s*(?:(?:19|20)\d{{2}}|atual|presente|momento)\b"
    rf")",
    re.IGNORECASE,
)

CONTRACT_KEYWORDS_RE = re.compile(
    r"\b(?:clt|pj|temporario|estagio|jovem\s+aprendiz|autonomo|freelancer|efetivo|tempo\s+integral|meio\s+periodo)\b",
    re.IGNORECASE,
)

ACTION_SENTENCE_START_RE = re.compile(
    r"^(?:atendimento|manutencao|administracao|desenvolvimento|levantamento|suporte\s+a|configuracao|"
    r"criacao|elaboracao|execucao|apoio|atuacao|implementacao|monitoramento|gestao\s+de\s+chamados|"
    r"organizacao|instalacao|diagnostico|realizacao|construcao|automacao|integracao|analise\s+de\s+dados)\b",
    re.IGNORECASE,
)


def _is_degree_line(line: str) -> bool:
    return bool(DEGREE_KEYWORDS_RE.search(_strip_accents(line)))


def _is_institution_line(line: str) -> bool:
    if INSTITUTION_ACRONYM_RE.search(line):
        return True
    return bool(INSTITUTION_WORD_RE.search(_strip_accents(line)))


def _is_period_only_line(line: str) -> bool:
    norm = _strip_accents(line).strip()
    if _is_degree_line(line) or _is_institution_line(line):
        return False
    return bool(PERIOD_LINE_RE.search(norm))


def _split_inline_education_segments(line: str) -> dict[str, str] | None:
    """Handles single-line entries such as `Tecnólogo em ADS — UNINTER · Concluído em Dez/2025`
    without ever splitting degree status suffixes like `Graduação em Sistemas - Em andamento`."""
    parts = [p.strip() for p in re.split(r"\s*(?:[—|]|·)\s*", line) if p.strip()]
    if len(parts) < 2:
        # Check hyphen split ONLY if one side is clearly an institution and the other is clearly a degree
        hyphen_parts = [p.strip() for p in re.split(r"\s+[-–]\s+", line) if p.strip()]
        if len(hyphen_parts) >= 2 and any(_is_institution_line(p) for p in hyphen_parts) and any(
            _is_degree_line(p) for p in hyphen_parts
        ):
            parts = hyphen_parts
        else:
            return None

    has_deg = any(_is_degree_line(p) for p in parts)
    has_inst = any(_is_institution_line(p) for p in parts)
    if not (has_deg and has_inst):
        return None

    deg = ""
    inst = ""
    per_parts: list[str] = []
    det_parts: list[str] = []
    for seg in parts:
        if not deg and _is_degree_line(seg):
            deg = seg
        elif not inst and _is_institution_line(seg):
            inst = seg
        elif PERIOD_LINE_RE.search(_strip_accents(seg)):
            per_parts.append(seg)
        else:
            det_parts.append(seg)

    return {
        "degree": deg,
        "institution": inst,
        "period": " · ".join(per_parts),
        "details": " · ".join(det_parts),
    }


def parse_education_blocks(lines_with_blanks: list[str]) -> list[EducationEntry]:
    """Groups multi-line academic entries (`Degree + Institution + Period`) into cohesive `EducationEntry` blocks."""
    entries: list[EducationEntry] = []
    current: dict[str, str] = {"degree": "", "institution": "", "period": "", "details": ""}

    def _flush() -> None:
        nonlocal current
        deg = current["degree"].strip()
        inst = current["institution"].strip()
        per = current["period"].strip()
        det = current["details"].strip()
        if deg or inst:
            entries.append(
                EducationEntry(
                    degree=(deg or inst)[:180],
                    institution=(inst if deg else "")[:180],
                    period=per[:140],
                    details=det[:600],
                )
            )
        current = {"degree": "", "institution": "", "period": "", "details": ""}

    for raw_ln in lines_with_blanks:
        ln = raw_ln.strip().lstrip("•*-– ")
        if not ln:
            if current["degree"] or current["institution"]:
                _flush()
            continue

        inline = _split_inline_education_segments(ln)
        if inline:
            if current["degree"] or current["institution"]:
                _flush()
            current = inline
            continue

        is_deg = _is_degree_line(ln)
        is_inst = _is_institution_line(ln) and not is_deg
        is_per = _is_period_only_line(ln)

        if is_deg:
            if current["degree"]:
                _flush()
            current["degree"] = ln
        elif is_inst:
            if current["institution"] and current["degree"]:
                _flush()
            current["institution"] = ln
        elif is_per:
            current["period"] = f"{current['period']} · {ln}".strip(" ·") if current["period"] else ln
        else:
            if not current["degree"]:
                current["degree"] = ln
            elif not current["institution"]:
                current["institution"] = ln
            else:
                current["details"] = f"{current['details']} · {ln}".strip(" ·") if current["details"] else ln

    if current["degree"] or current["institution"]:
        _flush()

    return entries[:10]


def _extract_location_token(text: str) -> str:
    loc_m = re.search(
        r"\b([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s]{2,30})\s*[,/-]\s*([A-Z]{2})\b",
        text,
    )
    if loc_m:
        return f"{loc_m.group(1).strip()}, {loc_m.group(2)}"
    if re.search(r"\bremoto\b", _strip_accents(text)):
        return "Remoto"
    return ""


def _is_experience_meta_line(line: str) -> bool:
    norm = _strip_accents(line)
    return bool(DATE_RANGE_RE.search(norm) or PERIOD_LINE_RE.search(norm) or CONTRACT_KEYWORDS_RE.search(norm))


def _looks_like_role_title(line: str) -> bool:
    if len(line) > 95 or line.endswith("."):
        return False
    norm = _strip_accents(line)
    if ACTION_SENTENCE_START_RE.search(norm):
        return False
    return True


def parse_experience_blocks(lines_with_blanks: list[str]) -> list[ExperienceEntry]:
    """Groups experience header lines (`Role + Company + Period/Location`) with all their activity lines."""
    non_empty_any = any(x.strip() for x in lines_with_blanks)
    if not non_empty_any:
        return []

    raw_blocks: list[list[str]] = []
    cur_lines: list[str] = []
    cur_has_body = False

    clean_seq = [ln.strip() for ln in lines_with_blanks]
    total = len(clean_seq)

    for idx, raw_ln in enumerate(clean_seq):
        if not raw_ln:
            continue
        was_preceded_by_blank = idx > 0 and clean_seq[idx - 1] == ""
        was_bullet = raw_ln.startswith(("•", "*", "- ", "– "))
        ln = raw_ln.lstrip("•*-– ").strip()
        if not ln:
            continue

        # Look ahead 1-2 non-empty lines to see if a new Role + Company/Date header is starting
        upcoming: list[str] = []
        for j in range(idx + 1, min(total, idx + 5)):
            if clean_seq[j]:
                upcoming.append(clean_seq[j].lstrip("•*-– ").strip())
            if len(upcoming) >= 2:
                break

        starts_new_exp = False
        if cur_lines and cur_has_body and not was_bullet:
            # Inline combined header with date range (`Cargo — Empresa · Mar/2024 – Set/2024`)
            if DATE_RANGE_RE.search(_strip_accents(ln)) and len(ln) <= 140 and ("—" in ln or "–" in ln or "|" in ln or "·" in ln):
                starts_new_exp = True
            elif was_preceded_by_blank and _looks_like_role_title(ln) and any(_is_experience_meta_line(u) for u in upcoming):
                starts_new_exp = True

        if starts_new_exp:
            raw_blocks.append(cur_lines)
            cur_lines = [raw_ln]
            cur_has_body = False
        else:
            cur_lines.append(raw_ln)
            if len(cur_lines) >= 3 and (was_bullet or len(ln) > 85 or ACTION_SENTENCE_START_RE.search(_strip_accents(ln))):
                cur_has_body = True

    if cur_lines:
        raw_blocks.append(cur_lines)

    results: list[ExperienceEntry] = []
    for block in raw_blocks[:12]:
        stripped = [b.lstrip("•*-– ").strip() for b in block if b.strip()]
        if not stripped:
            continue

        first = stripped[0]
        role = ""
        company = ""
        period = ""
        location = ""
        body_start = 1

        # Case 1: Combined single header line (`Cargo — Empresa · Período · Local`)
        header_segs = [s.strip() for s in re.split(r"\s*(?:[—|]|·)\s*", first) if s.strip()]
        if len(header_segs) == 1 and re.search(r"\s+[-–]\s+", first) and not DATE_RANGE_RE.search(_strip_accents(first)):
            header_segs = [s.strip() for s in re.split(r"\s+[-–]\s+", first, maxsplit=2) if s.strip()]

        if len(header_segs) >= 2:
            role = header_segs[0]
            per_parts: list[str] = []
            for seg in header_segs[1:]:
                loc_hit = _extract_location_token(seg)
                if loc_hit and seg.strip() == loc_hit:
                    location = loc_hit
                elif _is_experience_meta_line(seg):
                    per_parts.append(seg)
                elif not company:
                    company = seg
                else:
                    per_parts.append(seg)
            period = " · ".join(per_parts)
            # Check if line 2 is still a pure period/location meta line
            if len(stripped) >= 2 and _is_experience_meta_line(stripped[1]) and len(stripped[1]) <= 95:
                meta2 = stripped[1]
                loc2 = _extract_location_token(meta2)
                if loc2 and not location:
                    location = loc2
                period = f"{period} · {meta2}".strip(" ·") if period else meta2
                body_start = 2
        else:
            # Case 2: Multi-line header (`Line 1: Role`, `Line 2: Company`, `Line 3: Period/Contract`)
            role = first
            cursor = 1
            while cursor < min(len(stripped), 4):
                cand = stripped[cursor]
                if len(cand) > 100 or cand.endswith(".") or ACTION_SENTENCE_START_RE.search(_strip_accents(cand)):
                    break
                if _is_experience_meta_line(cand):
                    loc_hit = _extract_location_token(cand)
                    if loc_hit and not location:
                        location = loc_hit
                    period = f"{period} · {cand}".strip(" ·") if period else cand
                    cursor += 1
                elif not company and not block[cursor].startswith(("•", "*")):
                    company = cand
                    cursor += 1
                else:
                    break
            body_start = cursor

        body_lines = [b.lstrip("•*-– ").strip() for b in block[body_start:] if b.strip()]
        if not body_lines and len(stripped) == 1:
            body_lines = [first]

        results.append(
            ExperienceEntry(
                role=(role or "Experiência Profissional")[:160],
                company=company[:160],
                period=period[:120],
                location=location[:120],
                description="\n".join(body_lines)[:8000],
            )
        )

    return results


WORKLOAD_TOKEN_RE = re.compile(r"\b\d+\s*h(?:oras?)?\b", re.IGNORECASE)
COURSE_DATE_TOKEN_RE = re.compile(
    rf"\b(?:conclu[ií]do\s+em\s+)?(?:(?:{MONTH_TOKEN}|\d{{2}})\s*[/.-]\s*)?(?:19|20)\d{{2}}\b",
    re.IGNORECASE,
)


def normalize_course_fields(
    title: str,
    institution: str = "",
    workload: str = "",
    completion_date: str = "",
    description: str = "",
) -> CourseEntry:
    """Ensures workload (`40 horas`) and completion_date (`2026` / `Out/2026`) are cleanly separated."""
    wl = workload.strip()
    cdate = completion_date.strip()
    desc = description.strip()

    # If workload accidentally contains both workload and year (`40 horas · 2026`), split them cleanly
    if wl and not cdate:
        dt_m = COURSE_DATE_TOKEN_RE.search(_strip_accents(wl))
        if dt_m:
            raw_slice = wl[dt_m.start() : dt_m.end()]
            cdate = re.sub(r"^conclu[ií]do\s+em\s+", "", raw_slice, flags=re.IGNORECASE).strip()
            wl = (wl[: dt_m.start()] + " " + wl[dt_m.end() :]).strip(" ·•-–,;")

    return CourseEntry(
        title=title.strip()[:180] or "Curso",
        institution=institution.strip()[:160],
        workload=wl[:80],
        completion_date=cdate[:80],
        description=desc[:1000],
    )


def _parse_single_course_chunk(lines: list[str]) -> CourseEntry | None:
    cleaned = [x.lstrip("•*-– ").strip() for x in lines if x.strip()]
    if not cleaned:
        return None

    head = cleaned[0]
    extra_desc_lines = cleaned[1:]

    inline_desc = ""
    if ":" in head and not re.search(r"\b(?:inicio|conclusao|carga)\s*:", _strip_accents(head)):
        left, right = head.split(":", 1)
        if len(left.strip()) >= 3 and len(right.strip()) >= 8:
            head = left.strip()
            inline_desc = right.strip()

    workload = ""
    wl_m = WORKLOAD_TOKEN_RE.search(head)
    if wl_m:
        workload = wl_m.group(0).strip()
        head = (head[: wl_m.start()] + " " + head[wl_m.end() :]).strip()

    completion_date = ""
    dt_m = COURSE_DATE_TOKEN_RE.search(head)
    if dt_m:
        completion_date = re.sub(r"^conclu[ií]do\s+em\s+", "", dt_m.group(0), flags=re.IGNORECASE).strip()
        head = (head[: dt_m.start()] + " " + head[dt_m.end() :]).strip()

    segs = [
        s.strip(" ·•-–—,;")
        for s in re.split(r"\s*(?:[—|]|·|\s+[-–]\s+)\s*", head)
        if s.strip(" ·•-–—,;")
    ]
    title = segs[0] if segs else head.strip(" ·•-–—,;")
    institution = segs[1] if len(segs) >= 2 else ""

    # Check second line if it holds institution / workload / date
    remaining_desc: list[str] = []
    if inline_desc:
        remaining_desc.append(inline_desc)

    for sub in extra_desc_lines:
        sub_work = sub
        sub_wl = WORKLOAD_TOKEN_RE.search(sub_work)
        sub_dt = COURSE_DATE_TOKEN_RE.search(sub_work)
        if (sub_wl or sub_dt) and len(sub_work) <= 90:
            if sub_wl and not workload:
                workload = sub_wl.group(0).strip()
                sub_work = (sub_work[: sub_wl.start()] + " " + sub_work[sub_wl.end() :]).strip()
            dt2 = COURSE_DATE_TOKEN_RE.search(sub_work)
            if dt2 and not completion_date:
                completion_date = re.sub(r"^conclu[ií]do\s+em\s+", "", dt2.group(0), flags=re.IGNORECASE).strip()
                sub_work = (sub_work[: dt2.start()] + " " + sub_work[dt2.end() :]).strip()
            leftover_inst = sub_work.strip(" ·•-–—,;")
            if leftover_inst and not institution and len(leftover_inst) <= 70:
                institution = leftover_inst
        elif not institution and len(sub) <= 65 and not sub.endswith("."):
            institution = sub
        else:
            remaining_desc.append(sub)

    desc_joined = " ".join(remaining_desc).strip()
    if desc_joined and desc_joined[0].islower():
        desc_joined = desc_joined[0].upper() + desc_joined[1:]

    if len(title) < 2:
        return None
    return normalize_course_fields(
        title=title,
        institution=institution,
        workload=workload,
        completion_date=completion_date,
        description=desc_joined,
    )


def parse_course_blocks(lines_with_blanks: list[str]) -> list[CourseEntry]:
    """Parses `Cursos e Certificações` preserving Title, Institution, Workload, Completion Date, and Description."""
    has_blank_separators = "" in [x.strip() for x in lines_with_blanks if x is not None][1:-1]
    blocks: list[list[str]] = []

    if has_blank_separators:
        cur: list[str] = []
        for raw_ln in lines_with_blanks:
            if not raw_ln.strip():
                if cur:
                    blocks.append(cur)
                    cur = []
            else:
                cur.append(raw_ln)
        if cur:
            blocks.append(cur)
    else:
        for raw_ln in lines_with_blanks:
            s = raw_ln.strip()
            if not s:
                continue
            is_continuation = (
                blocks
                and not raw_ln.lstrip().startswith(("•", "*", "- "))
                and (WORKLOAD_TOKEN_RE.search(s) or COURSE_DATE_TOKEN_RE.search(s))
                and len(s) <= 75
                and not ("—" in s or " - " in s)
            )
            if is_continuation:
                blocks[-1].append(raw_ln)
            else:
                blocks.append([raw_ln])

    courses: list[CourseEntry] = []
    for blk in blocks[:15]:
        parsed = _parse_single_course_chunk(blk)
        if parsed:
            courses.append(parsed)
    return courses


def parse_project_blocks(lines_with_blanks: list[str]) -> list[ProjectEntry]:
    """Groups project name, optional HTTPS link, and description (zero `period_or_role`)."""
    blocks: list[list[str]] = []
    cur: list[str] = []
    for raw_ln in lines_with_blanks:
        s = raw_ln.strip()
        if not s:
            if cur:
                blocks.append(cur)
                cur = []
            continue
        if cur and raw_ln.lstrip().startswith(("•", "*")) and len(cur) >= 1:
            cur.append(s)
        elif cur and len(s) > 75:
            cur.append(s)
        elif not cur:
            cur.append(s)
        else:
            blocks.append(cur)
            cur = [s]
    if cur:
        blocks.append(cur)

    projects: list[ProjectEntry] = []
    for blk in blocks[:8]:
        head = blk[0].lstrip("•*-– ").strip()
        url_hit: str | None = None
        url_m = OUTBOUND_URL_RE.search(head)
        if url_m:
            url_hit = _normalize_https_url(url_m.group(0))
            head = (head[: url_m.start()] + " " + head[url_m.end() :]).strip(" —–-|·")

        segs = [x.strip() for x in re.split(r"\s*(?:[—|]|·)\s*", head, maxsplit=1) if x.strip()]
        name = segs[0] if segs else head
        desc_parts: list[str] = []
        if len(segs) >= 2 and not PERIOD_LINE_RE.search(_strip_accents(segs[1])):
            desc_parts.append(segs[1])

        for extra in blk[1:]:
            ex_clean = extra.lstrip("•*-– ").strip()
            if not url_hit:
                um2 = OUTBOUND_URL_RE.search(ex_clean)
                if um2:
                    url_hit = _normalize_https_url(um2.group(0))
            desc_parts.append(ex_clean)

        if len(name) >= 2:
            projects.append(
                ProjectEntry(
                    name=name[:160],
                    url=url_hit,
                    description="\n".join(desc_parts)[:3000],
                )
            )
    return projects


def heuristic_parse_resume_text(raw_text: str, embedded_urls: list[str]) -> MasterProfile:
    contacts = _extract_contacts_from_raw_text(raw_text, embedded_urls)
    raw_lines = [ln.strip() for ln in raw_text.splitlines()]
    non_empty_lines = [ln for ln in raw_lines if ln]

    # Infer candidate name from the first non-contact, non-heading line
    candidate_name = "Candidato Importado"
    name_line_value: str | None = None
    for ln in non_empty_lines[:6]:
        if (
            "@" in ln
            or OUTBOUND_PHONE_RE.search(ln)
            or "http" in ln.lower()
            or "linkedin.com" in ln.lower()
            or "github.com" in ln.lower()
        ):
            continue
        norm_ln = _norm_heading(ln)
        if any(norm_ln in aliases for aliases in SECTION_HEADING_MAP.values()):
            break
        if 3 <= len(ln) <= 90:
            candidate_name = ln.strip(" -|•")
            name_line_value = ln
            break

    buckets: dict[str, list[str]] = {k: [] for k in SECTION_HEADING_MAP}
    current_bucket: str | None = None
    preamble_lines: list[str] = []
    passed_name = name_line_value is None

    for ln in raw_lines:
        if not passed_name:
            if ln == name_line_value:
                passed_name = True
            continue

        if not ln:
            if current_bucket is not None and buckets[current_bucket] and buckets[current_bucket][-1] != "":
                buckets[current_bucket].append("")
            continue

        norm_ln = _norm_heading(ln)
        matched_section: str | None = None
        if len(ln) <= 55:
            for sec_key, aliases in SECTION_HEADING_MAP.items():
                if norm_ln in aliases:
                    matched_section = sec_key
                    break
        if matched_section:
            current_bucket = matched_section
            continue

        if current_bucket is None:
            if (
                "@" not in ln
                and not OUTBOUND_PHONE_RE.search(ln)
                and "linkedin.com" not in ln.lower()
                and "github.com" not in ln.lower()
            ):
                preamble_lines.append(ln)
        else:
            buckets[current_bucket].append(ln)

    city = ""
    state = ""
    for pln in preamble_lines[:4]:
        loc_m = re.search(r"([A-Za-zÀ-ÿ\s]{3,35})\s*[-/,·]\s*([A-Z]{2})\b", pln)
        if loc_m:
            city = loc_m.group(1).strip(" ,·-")
            state = loc_m.group(2).strip()
            break

    summary_lines = [s.lstrip("•*-– ").strip() for s in buckets["summary"] if s.strip()]
    summary_text = "\n".join(summary_lines).strip()
    if not summary_text and len(preamble_lines) >= 1:
        long_preamble = [p for p in preamble_lines if len(p) > 65]
        summary_text = "\n".join(long_preamble).strip()

    experiences = parse_experience_blocks(buckets["experience"])
    education = parse_education_blocks(buckets["education"])
    courses = parse_course_blocks(buckets["courses"])
    projects = parse_project_blocks(buckets["projects"])

    skills: list[str] = []
    for sk_ln in buckets["skills"]:
        if not sk_ln.strip():
            continue
        cleaned_sk = sk_ln.lstrip("•*-– ").strip()
        cleaned_sk = (
            cleaned_sk.split(":", 1)[-1]
            if ":" in cleaned_sk and len(cleaned_sk.split(":", 1)[0]) < 35
            else cleaned_sk
        )
        for token in re.split(r"[,;•·|]|\s+e\s+(?=[A-Z])", cleaned_sk):
            tag = token.strip(" .")
            if 1 < len(tag) <= 80:
                skills.append(tag)

    languages: list[LanguageEntry] = []
    for l_ln in buckets["languages"]:
        if not l_ln.strip():
            continue
        for chunk in re.split(r"[•·|;]", l_ln.lstrip("•*-– ").strip()):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                lang, prof = chunk.split(":", 1)
                languages.append(LanguageEntry(language=lang.strip()[:80], proficiency=prof.strip(" .")[:80]))
            elif "-" in chunk or "–" in chunk or "—" in chunk:
                lang, prof = re.split(r"[-–—]", chunk, maxsplit=1)
                languages.append(LanguageEntry(language=lang.strip()[:80], proficiency=prof.strip(" .")[:80]))
            else:
                languages.append(LanguageEntry(language=chunk[:80], proficiency=""))

    return MasterProfile(
        header=CandidateHeader(
            full_name=candidate_name[:140],
            email=contacts["email"],
            phone=contacts["phone"],
            city=city,
            state=state,
            links=contacts["links"],
        ),
        summary=summary_text[:4000],
        experiences=experiences,
        education=education,
        courses=courses,
        projects=projects,
        skills=skills,
        languages=languages,
    )


def _ai_education_looks_fragmented(ai_edu: list[EducationEntry], heuristic_edu: list[EducationEntry]) -> bool:
    if not ai_edu and heuristic_edu:
        return True
    # Detect line-by-line fragmentation where institutions or periods became standalone `degree` records
    for item in ai_edu:
        if not item.institution and (_is_institution_line(item.degree) or _is_period_only_line(item.degree)):
            return True
    return False


async def import_profile_from_uploaded_file(
    *,
    filename: str,
    raw_bytes: bytes,
    settings: AppSettings,
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    if not raw_bytes:
        raise ProfileImportError("O arquivo enviado está vazio.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ProfileImportError("O arquivo excede o limite máximo de 10 MB.")

    suffix = Path(filename or "").suffix.lower()
    if suffix == ".pdf":
        raw_text, embedded_urls = extract_text_and_links_from_pdf(raw_bytes)
    elif suffix == ".docx":
        raw_text, embedded_urls = extract_text_and_links_from_docx(raw_bytes)
    else:
        raise ProfileImportError("Formato não suportado. Envie um arquivo .pdf ou .docx.")

    if len(raw_text.strip()) < 20:
        raise ProfileImportError(
            "Não foi possível extrair texto legível do documento (verifique se o PDF não é apenas imagem escaneada)."
        )

    contacts = _extract_contacts_from_raw_text(raw_text, embedded_urls)
    heuristic_draft = heuristic_parse_resume_text(raw_text, embedded_urls)
    warnings: list[str] = []

    if settings.ai_connected:
        try:
            adapter = build_llm_adapter(settings=settings, transport=llm_transport)
            masked_text, placeholder_map = strip_pii_for_profile_import(raw_text)
            llm_res = await adapter.extract_profile_from_text(masked_text)
            data = llm_res.data

            def _rh(val: Any) -> str:
                return rehydrate_text(str(val or ""), placeholder_map).strip()

            full_name = _rh(data.get("full_name")) or heuristic_draft.header.full_name
            ai_education = [
                EducationEntry(
                    degree=_rh(item.get("degree"))[:180],
                    institution=_rh(item.get("institution"))[:180],
                    period=_rh(item.get("period"))[:140],
                    details=_rh(item.get("details"))[:600],
                )
                for item in (data.get("education") or [])
                if isinstance(item, dict) and _rh(item.get("degree"))
            ]
            if _ai_education_looks_fragmented(ai_education, heuristic_draft.education):
                ai_education = heuristic_draft.education

            ai_courses = [
                normalize_course_fields(
                    title=_rh(item.get("title")),
                    institution=_rh(item.get("institution")),
                    workload=_rh(item.get("workload")),
                    completion_date=_rh(item.get("completion_date")),
                    description=_rh(item.get("description")),
                )
                for item in (data.get("courses") or [])
                if isinstance(item, dict) and _rh(item.get("title"))
            ]

            draft = MasterProfile(
                header=CandidateHeader(
                    full_name=full_name[:140],
                    email=contacts["email"],
                    phone=contacts["phone"],
                    city=(_rh(data.get("city")) or heuristic_draft.header.city)[:90],
                    state=(_rh(data.get("state")) or heuristic_draft.header.state)[:90],
                    links=contacts["links"],
                ),
                summary=(_rh(data.get("summary")) or heuristic_draft.summary)[:4000],
                experiences=[
                    ExperienceEntry(
                        role=_rh(item.get("role"))[:160] or "Experiência",
                        company=_rh(item.get("company"))[:160],
                        period=_rh(item.get("period"))[:120],
                        location=_rh(item.get("location"))[:120],
                        description=_rh(item.get("description"))[:8000],
                    )
                    for item in (data.get("experiences") or [])
                    if isinstance(item, dict) and (_rh(item.get("role")) or _rh(item.get("description")))
                ]
                or heuristic_draft.experiences,
                education=ai_education,
                courses=ai_courses or heuristic_draft.courses,
                projects=[
                    ProjectEntry(
                        name=_rh(item.get("name"))[:160],
                        url=_normalize_https_url(_rh(item.get("url"))),
                        description=_rh(item.get("description"))[:3000],
                    )
                    for item in (data.get("projects") or [])
                    if isinstance(item, dict) and _rh(item.get("name"))
                ],
                skills=[_rh(s) for s in (data.get("skills") or []) if _rh(s)] or heuristic_draft.skills,
                languages=[
                    LanguageEntry(
                        language=_rh(item.get("language"))[:80],
                        proficiency=_rh(item.get("proficiency"))[:80],
                    )
                    for item in (data.get("languages") or [])
                    if isinstance(item, dict) and _rh(item.get("language"))
                ]
                or heuristic_draft.languages,
            )
            return {
                "extraction_mode": "ai",
                "provider": llm_res.provider,
                "model": llm_res.model,
                "token_usage": llm_res.token_usage.model_dump(mode="json"),
                "extracted_chars": len(raw_text),
                "warnings": warnings,
                "profile_draft": draft.model_dump(mode="json"),
            }
        except LlmAdapterError as exc:
            warnings.append(
                f"Extração via Google Gemini indisponível ({exc}); aplicado agrupamento estrutural automático para revisão."
            )
    else:
        warnings.append(
            "Conexão com o Google ainda não ativada; aplicado agrupamento estrutural inteligente. Revise os campos antes de salvar."
        )

    return {
        "extraction_mode": "heuristic_fallback",
        "provider": None,
        "model": None,
        "token_usage": None,
        "extracted_chars": len(raw_text),
        "warnings": warnings,
        "profile_draft": heuristic_draft.model_dump(mode="json"),
    }
