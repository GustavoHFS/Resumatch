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
from urllib.parse import urlsplit

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


def _canonical_url_key(url: str) -> str:
    """Stable dedupe key: lower-case host without leading ``www.`` and trailing slash."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url.strip().lower()

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    path = parts.path.rstrip("/")
    key = f"{parts.scheme.lower()}://{netloc}{path}"
    if parts.query:
        key += f"?{parts.query}"
    if parts.fragment:
        key += f"#{parts.fragment}"
    return key


def _dedupe_urls(urls: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not url:
            continue
        key = _canonical_url_key(url)
        if key not in seen:
            seen.add(key)
            unique.append(url)
    return unique


def _infer_link_label(url: str) -> str:
    low = url.lower()
    if "github.com" in low:
        return "GitHub"
    if "linkedin.com" in low:
        return "LinkedIn"
    if "gitlab.com" in low:
        return "GitLab"
    return "Portfólio"


_PDF_CONTINUATION_CONNECTORS = {
    "a", "à", "às", "ao", "aos", "as", "o", "os", "um", "uma", "uns", "umas",
    "de", "da", "das", "do", "dos", "em", "na", "nas", "no", "nos",
    "e", "ou", "que", "como", "com", "por", "para", "sem", "sob", "sobre",
    "entre", "após", "ate", "até", "desde", "durante", "nos", "pelos", "pela",
    "pelo", "nas", "num", "numa", "se", "quando", "onde", "cujo", "cuja",
}

_PDF_BULLET_PREFIXES = ("•", "*", "- ", "– ", "— ")


def _should_rejoin_pdf_lines(previous: str, following: str) -> bool:
    """Only joins an unmistakable wrapped sentence continuation.

    A continuation needs both a dangling connector/comma on the previous line and a
    lower-case start on the next one. Headings, record headers, metadata lines and
    distinct bullets keep their hard break.
    """
    prev = previous.rstrip()
    nxt = following.lstrip()
    if not prev or not nxt:
        return False
    if nxt.startswith(_PDF_BULLET_PREFIXES):
        return False
    if not nxt[0].islower():
        return False
    if prev.endswith((":", ";", ".", "!", "?", "(", "[")):
        return False
    if prev.endswith(","):
        return True

    last_word = re.split(r"\s+", prev)[-1].strip("([\"'").casefold()
    last_word = _strip_accents(last_word)
    return last_word in _PDF_CONTINUATION_CONNECTORS


def _rejoin_pdf_line_breaks(text: str) -> str:
    """Rebuilds visual PDF wraps without ever merging paragraphs, pages or bullets."""
    output: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            output.append("")
            continue
        if output and output[-1].strip() and _should_rejoin_pdf_lines(output[-1], line):
            output[-1] = output[-1].rstrip() + " " + line.lstrip()
        else:
            output.append(line)
    return "\n".join(output)


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

    joined = unicodedata.normalize("NFKC", "\n\n".join(pages_text).strip())
    return _rejoin_pdf_line_breaks(joined), _dedupe_urls(uris)


def _extract_paragraph_text(p_elem: ET.Element) -> str:
    parts: list[str] = []
    for node in p_elem.iter():
        if node.tag == f"{WORD_NS}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{WORD_NS}tab":
            parts.append(" ")
        elif node.tag == f"{WORD_NS}br":
            parts.append("\n")
    return unicodedata.normalize("NFKC", "".join(parts)).strip()


def _cell_blocks(cell: ET.Element) -> list[str]:
    """Paragraph blocks of a table cell, recursing into wrappers but never duplicating nested tables."""
    blocks: list[str] = []
    for child in list(cell):
        if child.tag == f"{WORD_NS}p":
            txt = _extract_paragraph_text(child)
            if txt:
                blocks.append(txt)
        elif child.tag == f"{WORD_NS}tbl":
            blocks.extend(_table_blocks(child))
        else:
            blocks.extend(_cell_blocks(child))
    return blocks


def _row_cells(row: ET.Element) -> list[list[str]]:
    cells: list[list[str]] = []
    for child in list(row):
        if child.tag == f"{WORD_NS}tc":
            cells.append(_cell_blocks(child))
        elif child.tag == f"{WORD_NS}tbl":
            continue
        elif child.tag != f"{WORD_NS}p":
            cells.extend(_row_cells(child))
    return cells


def _table_rows(table: ET.Element) -> list[list[list[str]]]:
    """Returns rows of cells, each cell as its own ordered paragraph blocks.

    Only direct rows are read; nested tables are handled inside their own cell so
    that ``.//w:tr``/``.//w:p`` descendant searches do not duplicate their text.
    """
    rows: list[list[list[str]]] = []
    for child in list(table):
        if child.tag == f"{WORD_NS}tr":
            rows.append(_row_cells(child))
        elif child.tag == f"{WORD_NS}tbl":
            continue
        elif child.tag != f"{WORD_NS}p":
            rows.extend(_table_rows(child))
    return rows


def _table_blocks(table: ET.Element) -> list[str]:
    rows = _table_rows(table)
    if not rows:
        return []

    max_columns = max((len(row) for row in rows), default=0)
    lines: list[str] = []
    if max_columns == 2:
        # Two-column Word/Canva templates (sidebar + main body): read the whole
        # left column first, then the right column, keeping paragraph order.
        for column in range(2):
            for row in rows:
                if column < len(row):
                    lines.extend(row[column])
    else:
        # One-column tables and irregular grids keep their natural row order.
        for row in rows:
            for cell in row:
                lines.extend(cell)
    return lines


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
            lines.extend(_table_blocks(child))
        elif child.tag not in (f"{WORD_NS}sectPr",):
            lines.extend(_cell_blocks(child))
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

    return unicodedata.normalize("NFKC", "\n".join(blocks)).strip(), _dedupe_urls(uris)


def _extract_contacts_from_raw_text(raw_text: str, embedded_urls: list[str]) -> dict[str, Any]:
    email_match = OUTBOUND_EMAIL_RE.search(raw_text)
    phone_match = OUTBOUND_PHONE_RE.search(raw_text)

    text_urls = [_normalize_https_url(m.group(0)) for m in OUTBOUND_URL_RE.finditer(raw_text)]
    candidates = [u for u in [*embedded_urls, *(u for u in text_urls if u)] if u and "mailto:" not in u.lower()]
    all_urls = _dedupe_urls(candidates)

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
    r"SENAI|SENAC|FIAP|FGV|IBMEC|INSPER|IME|ITA|"
    r"FAFIRE|UNIBRA|FBV|FPS|UFPB|UFCG|UTFPR|UNIFESP"
    r")\b"
)

INSTITUTION_WORD_RE = re.compile(
    r"\b(?:"
    r"universidade|centro\s+universitario|faculdade|instituto\s+federal|"
    r"instituto|escola\s+tecnica|colegio|fundacao|cesar\s+school|"
    r"mackenzie|estacio|anhanguera|pitagoras|unopar|unicesumar|uninassau|unicap|uninter|etemac|"
    r"fafire|unibra|fbv|fps|ufpb|ufcg|utfpr|unifesp"
    r")\b",
    re.IGNORECASE,
)

MONTH_TOKEN = (
    r"(?:jan(?:eiro)?|fev(?:ereiro)?|mar(?:co|ço)?|abr(?:il)?|mai(?:o)?|"
    r"jun(?:ho)?|jul(?:ho)?|ago(?:sto)?|set(?:embro)?|out(?:ubro)?|"
    r"nov(?:embro)?|dez(?:embro)?|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"sept|dec)"
)

# One date point: `Março de 2023`, `Mar/2023`, `03/2022`, `Dezembro 2027` or a bare year.
_DATE_POINT = rf"(?:(?:(?:{MONTH_TOKEN})(?:\s+de)?|\d{{1,2}})\s*[/.\s-]\s*)?(?:19|20)\d{{2}}"

DATE_RANGE_RE = re.compile(
    rf"(?:\bde\s+)?{_DATE_POINT}\s*(?:\s*[-–—]\s*|\s+a\s+|\s+até\s+)\s*"
    rf"(?:{_DATE_POINT}|atual|presente|momento|o\s+momento|em\s+andamento|andamento)\b",
    re.IGNORECASE,
)

PERIOD_LINE_RE = re.compile(
    rf"(?:"
    rf"\bin[ií]cio\s*:|\bprevis[aã]o\s+de\s+conclus[aã]o|\bconclu[ií]do\s+em\b|"
    rf"\bconclus[aã]o\s*:|\bcompleted?\s+in\b|"
    rf"(?:{DATE_RANGE_RE.pattern})|"
    rf"\b(?:{MONTH_TOKEN})(?:\s+de)?\s*[/.\s-]\s*(?:19|20)\d{{2}}\b|"
    rf"\b(?:19|20)\d{{2}}\s*[-–—]\s*(?:(?:19|20)\d{{2}}|atual|presente|andamento)\b|"
    rf"^(?:em\s+andamento|cursando)$|"
    rf"^(?:19|20)\d{{2}}$"
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


def _split_preserving_date_ranges(line: str, pattern: str) -> list[str]:
    """Splits an inline record without cutting through a date range such as
    `Março de 2023 – Dezembro de 2027`."""
    protected: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    temp = DATE_RANGE_RE.sub(_protect, line)
    parts = [p.strip() for p in re.split(pattern, temp) if p.strip()]

    def _restore(value: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], value)

    return [_restore(part) for part in parts]


def _split_inline_education_segments(line: str) -> dict[str, str] | None:
    """Handles single-line entries such as `Tecnólogo em ADS — UNINTER · Concluído em Dez/2025`
    without ever splitting degree status suffixes like `Graduação em Sistemas - Em andamento`."""
    parts = _split_preserving_date_ranges(line, r"\s*(?:[—|]|·)\s*")
    if len(parts) < 2:
        # Check hyphen split ONLY if one side is clearly an institution and the other is clearly a degree
        hyphen_parts = _split_preserving_date_ranges(line, r"\s+[-–]\s+")
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
            # An anticipated institution waits for its degree instead of being
            # flushed as a standalone (and later overwritten) entry.
            if current["degree"] or (current["institution"] and current["period"]):
                _flush()
            continue

        inline = _split_inline_education_segments(ln)
        if inline:
            if current["degree"] or current["institution"]:
                _flush()
            current = inline
            continue

        is_deg = _is_degree_line(ln)
        is_per = _is_period_only_line(ln)
        is_inst = _is_institution_line(ln) and not is_deg and not is_per

        if is_deg:
            if current["degree"]:
                _flush()
            current["degree"] = ln
        elif is_per:
            current["period"] = f"{current['period']} · {ln}".strip(" ·") if current["period"] else ln
        elif is_inst:
            if current["institution"] and current["degree"]:
                # Consecutive entries without a blank line: close the previous one.
                _flush()
                current["institution"] = ln
            elif current["institution"]:
                # Anticipated institution must never be overwritten; keep both texts.
                current["details"] = (
                    f"{current['details']} · {ln}".strip(" ·") if current["details"] else ln
                )
            else:
                current["institution"] = ln
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


_STATE_NAME_TO_UF: dict[str, str] = {
    "acre": "AC",
    "alagoas": "AL",
    "amapa": "AP",
    "amazonas": "AM",
    "bahia": "BA",
    "ceara": "CE",
    "distrito federal": "DF",
    "espirito santo": "ES",
    "goias": "GO",
    "maranhao": "MA",
    "mato grosso": "MT",
    "mato grosso do sul": "MS",
    "minas gerais": "MG",
    "para": "PA",
    "paraiba": "PB",
    "parana": "PR",
    "pernambuco": "PE",
    "piaui": "PI",
    "rio de janeiro": "RJ",
    "rio grande do norte": "RN",
    "rio grande do sul": "RS",
    "rondonia": "RO",
    "roraima": "RR",
    "santa catarina": "SC",
    "sao paulo": "SP",
    "sergipe": "SE",
    "tocantins": "TO",
}

_STATE_NAMES: tuple[str, ...] = (
    "Acre",
    "Alagoas",
    "Amapá",
    "Amazonas",
    "Bahia",
    "Ceará",
    "Distrito Federal",
    "Espírito Santo",
    "Goiás",
    "Maranhão",
    "Mato Grosso",
    "Mato Grosso do Sul",
    "Minas Gerais",
    "Pará",
    "Paraíba",
    "Paraná",
    "Pernambuco",
    "Piauí",
    "Rio de Janeiro",
    "Rio Grande do Norte",
    "Rio Grande do Sul",
    "Rondônia",
    "Roraima",
    "Santa Catarina",
    "São Paulo",
    "Sergipe",
    "Tocantins",
)

_UF_CODES = tuple(sorted(set(_STATE_NAME_TO_UF.values())))
_STATE_ALTERNATION = "|".join(
    re.escape(name)
    for name in sorted({*_STATE_NAMES, *_STATE_NAME_TO_UF}, key=len, reverse=True)
)
_UF_ALTERNATION = "|".join(_UF_CODES)

_CITY_LABEL_RE = re.compile(
    r"^(?:cidade|local(?:iza[cç][aã]o)?|endere[cç]o|resid[eê]ncia)\s*:\s*",
    re.IGNORECASE,
)
_CITY_CONTEXT_PREFIX_RE = re.compile(
    r"^(?:natural\s+de|residente\s+em|residindo\s+em|localizad[oa]\s+em|mora\s+em|based\s+in)\s+",
    re.IGNORECASE,
)
_CITY_STATE_FIELD_RE = re.compile(
    rf"^(?P<city>.+?)\s*(?:[-–—,/]|\s+)\s*(?P<state>{_UF_ALTERNATION}|{_STATE_ALTERNATION})\s*$",
    re.IGNORECASE,
)


def _normalize_state_token(token: str) -> str:
    cleaned = token.strip(" .,;:-–—")
    upper = cleaned.upper()
    if upper in _UF_CODES:
        return upper
    return _STATE_NAME_TO_UF.get(_strip_accents(cleaned).casefold(), "")


def _clean_city_name(value: str) -> str:
    city = _CITY_LABEL_RE.sub("", value.strip())
    city = _CITY_CONTEXT_PREFIX_RE.sub("", city)
    city = city.strip(" .,;:|/-–—·")
    city = re.sub(r"\s+", " ", city)
    if not (2 <= len(city) <= 60):
        return ""
    if any(ch.isdigit() for ch in city) or "@" in city:
        return ""
    lowered = _strip_accents(city).casefold()
    if "http" in lowered or "www." in lowered or lowered in {"brasil", "remoto"}:
        return ""
    if not re.fullmatch(r"[A-Za-zÀ-ÿ' .-]+", city):
        return ""
    return city


def _city_state_from_field(field: str) -> tuple[str, str] | None:
    cleaned_field = re.sub(r"[\s,–—-]+brasil\s*$", "", field.strip(), flags=re.IGNORECASE)
    match = _CITY_STATE_FIELD_RE.match(cleaned_field)
    if not match:
        return None
    city = _clean_city_name(match.group("city"))
    uf = _normalize_state_token(match.group("state"))
    if city and uf:
        return city, uf
    return None


def _extract_city_state(text: str) -> tuple[str, str]:
    """Finds city/state across the whole preamble, including mixed contact lines."""
    for line in text.splitlines():
        # Contact tokens are separators for location purposes; they must never
        # become part of the city or leak into the summary.
        sanitized = OUTBOUND_EMAIL_RE.sub(" | ", line)
        sanitized = OUTBOUND_PHONE_RE.sub(" | ", sanitized)
        sanitized = OUTBOUND_URL_RE.sub(" | ", sanitized)
        for field in re.split(r"[|;•·]", sanitized):
            parsed = _city_state_from_field(field)
            if parsed:
                return parsed
    return "", ""


def _extract_location_token(text: str) -> str:
    parsed = _city_state_from_field(text)
    if parsed:
        return f"{parsed[0]}, {parsed[1]}"
    if re.search(r"\bremoto\b", _strip_accents(text)):
        return "Remoto"
    return ""


def _is_experience_meta_line(line: str) -> bool:
    norm = _strip_accents(line)
    return bool(DATE_RANGE_RE.search(norm) or PERIOD_LINE_RE.search(norm) or CONTRACT_KEYWORDS_RE.search(norm))


_EXPERIENCE_LABEL_RE = re.compile(
    r"^(?:local(?:iza[cç][aã]o)?|cidade|contrato|regime|modalidade|modelo|jornada|"
    r"hor[aá]rio|sal[aá]rio|benef[ií]cios|atividades|responsabilidades|projetos)\s*:",
    re.IGNORECASE,
)


def _looks_like_role_title(line: str) -> bool:
    if len(line) > 95 or line.endswith("."):
        return False
    if not line or not line[0].isupper():
        return False
    if ":" in line or "@" in line or "http" in line.casefold():
        return False
    if _EXPERIENCE_LABEL_RE.search(line):
        return False
    norm = _strip_accents(line)
    if ACTION_SENTENCE_START_RE.search(norm):
        return False
    if _is_institution_line(line):
        return False
    return True


def _looks_like_company_line(line: str) -> bool:
    if not line or not line[0].isupper():
        return False
    if len(line) > 110 or line.endswith("."):
        return False
    if ":" in line or line.startswith(("•", "*", "- ", "– ")):
        return False
    norm = _strip_accents(line)
    if ACTION_SENTENCE_START_RE.search(norm):
        return False
    if _is_experience_meta_line(line):
        return False
    return True


def _split_company_period(line: str) -> tuple[str, str]:
    """Splits `Empresa Beta · Mar/2024 – Atual` without cutting the date range."""
    parts = _split_preserving_date_ranges(
        line, r"\s*(?:[—|]|·)\s*|\s+[-–]\s+"
    )
    company = ""
    period_parts: list[str] = []
    for part in parts:
        if _is_experience_meta_line(part):
            period_parts.append(part)
        elif not company and _looks_like_company_line(part):
            company = part
        else:
            period_parts.append(part)
    return company, " · ".join(period_parts)


def _block_has_experience_header(lines: list[str]) -> bool:
    stripped = [ln.lstrip("•*-– ").strip() for ln in lines if ln.strip()]
    if not stripped:
        return False
    first = stripped[0]
    if DATE_RANGE_RE.search(_strip_accents(first)) and any(
        sep in first for sep in ("—", "–", "|", "·", " - ")
    ):
        return True
    return any(_has_temporal_evidence(cand) for cand in stripped[1:4])


def _has_temporal_evidence(line: str) -> bool:
    norm = _strip_accents(line)
    return bool(DATE_RANGE_RE.search(norm) or PERIOD_LINE_RE.search(norm))


def _has_inline_experience_header(line: str) -> bool:
    if len(line) > 140 or not any(sep in line for sep in ("—", "–", "|", "·", " - ")):
        return False
    if not DATE_RANGE_RE.search(_strip_accents(line)):
        return False
    first_seg = re.split(r"\s*(?:[—|]|·)\s*", line, maxsplit=1)[0].strip()
    return bool(first_seg) and _looks_like_role_title(first_seg) and not _is_experience_meta_line(first_seg)


def _starts_experience_header(lines: list[str], index: int) -> bool:
    """Detects a coherent `Cargo → Empresa → Período` header, with or without a blank line.

    Temporal evidence is mandatory, so activity bullets, short descriptions and the
    current job's metadata never open a phantom experience.
    """
    line = lines[index].lstrip("•*-– ").strip()
    if not line:
        return False
    if _has_inline_experience_header(line):
        return True
    if _is_experience_meta_line(line) or _extract_location_token(line):
        return False
    if not _looks_like_role_title(line):
        return False

    upcoming: list[str] = []
    for j in range(index + 1, len(lines)):
        candidate = lines[j].lstrip("•*-– ").strip()
        if not candidate:
            continue
        upcoming.append(candidate)
        if len(upcoming) >= 3:
            break
    if not upcoming:
        return False

    meta_index = next(
        (pos for pos, cand in enumerate(upcoming) if _has_temporal_evidence(cand) and len(cand) <= 110),
        None,
    )
    if meta_index is None:
        return False
    if meta_index == 0:
        # Two-line header where company and period share the second line.
        company, _period = _split_company_period(upcoming[0])
        return bool(company)
    return _looks_like_company_line(upcoming[0])


def parse_experience_blocks(lines_with_blanks: list[str]) -> list[ExperienceEntry]:
    """Groups experience header lines (`Role + Company + Period/Location`) with all their activity lines."""
    non_empty_any = any(x.strip() for x in lines_with_blanks)
    if not non_empty_any:
        return []

    raw_blocks: list[list[str]] = []
    cur_lines: list[str] = []

    clean_seq = [ln.strip() for ln in lines_with_blanks]
    total = len(clean_seq)

    for idx, raw_ln in enumerate(clean_seq):
        if not raw_ln:
            continue
        was_bullet = raw_ln.startswith(("•", "*", "- ", "– "))
        ln = raw_ln.lstrip("•*-– ").strip()
        if not ln:
            continue

        starts_new_exp = False
        if cur_lines and not was_bullet and _block_has_experience_header(cur_lines):
            # Works with or without blank lines and even when the current job has
            # no description at all: structure + temporal evidence are required.
            starts_new_exp = _starts_experience_header(clean_seq, idx)

        if starts_new_exp:
            raw_blocks.append(cur_lines)
            cur_lines = [raw_ln]
        else:
            cur_lines.append(raw_ln)

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
        header_segs = _split_preserving_date_ranges(first, r"\s*(?:[—|]|·)\s*")
        if len(header_segs) == 1 and DATE_RANGE_RE.search(_strip_accents(first)):
            # `Cargo - Empresa - Mar/2024 - Set/2024`: split hyphens without
            # cutting the date range itself.
            hyphen_segs = _split_preserving_date_ranges(first, r"\s+[-–]\s+")
            if len(hyphen_segs) >= 2:
                header_segs = hyphen_segs

        if len(header_segs) >= 2:
            role = header_segs[0]
            per_parts: list[str] = []
            for seg in header_segs[1:]:
                loc_hit = _extract_location_token(seg)
                if loc_hit:
                    location = location or loc_hit
                    if _is_experience_meta_line(seg):
                        per_parts.append(seg)
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
                loc_hit = _extract_location_token(cand)
                if loc_hit and not _is_experience_meta_line(cand):
                    location = location or loc_hit
                    cursor += 1
                elif _is_experience_meta_line(cand):
                    if loc_hit and not location:
                        location = loc_hit
                    company_from_meta, period_from_meta = _split_company_period(cand)
                    if company_from_meta and not company:
                        company = company_from_meta
                    period_value = period_from_meta or cand
                    period = f"{period} · {period_value}".strip(" ·") if period else period_value
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
    rf"\b(?:conclu[ií]do\s+em\s+)?{_DATE_POINT}\b",
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


_COURSE_DESCRIPTION_WORDS = {
    "formacao", "inteligencia", "artificial", "generativa", "generativo", "automacao",
    "fluxos", "validacao", "dados", "completo", "completa", "intensivo", "intensiva",
    "pratico", "pratica", "online", "remoto", "remota", "avancado", "avancada",
    "basico", "basica", "iniciante", "intermediario", "intermediaria", "conteudo",
    "modulo", "aulas", "projetos", "aplicada", "aplicado",
}


def _is_course_institution_candidate(text: str) -> bool:
    cleaned = text.strip(" ·•-–—,;")
    if not cleaned:
        return False
    if _is_institution_line(cleaned):
        return True
    if not (2 <= len(cleaned) <= 70) or cleaned.endswith((".", "!", "?")):
        return False
    if not cleaned[0].isupper() or ACTION_SENTENCE_START_RE.search(_strip_accents(cleaned)):
        return False
    words = cleaned.split()
    if len(words) > 4:
        return False
    normalized_words = {_strip_accents(word).casefold().strip("(),.") for word in words}
    if normalized_words & _COURSE_DESCRIPTION_WORDS:
        return False
    return True


def _looks_like_course_title(line: str) -> bool:
    cleaned = line.strip()
    if not cleaned or len(cleaned) > 100 or cleaned.endswith((".", "!", "?")):
        return False
    if not cleaned[0].isupper() or ACTION_SENTENCE_START_RE.search(_strip_accents(cleaned)):
        return False
    words = cleaned.split()
    if len(words) > 10:
        return False
    if len(words) == 1:
        return True
    capitalized = sum(1 for word in words if word[:1].isupper())
    return capitalized >= max(2, (len(words) + 1) // 2)


def _is_course_continuation(line: str, blocks: list[list[str]]) -> bool:
    if not blocks or not line.strip():
        return False
    cleaned = line.strip()
    has_new_course_structure = bool(
        (WORKLOAD_TOKEN_RE.search(cleaned) or COURSE_DATE_TOKEN_RE.search(cleaned))
        and any(sep in cleaned for sep in ("—", "|"))
    ) or _is_institution_line(cleaned)

    if (cleaned.endswith((".", "!", "?")) or len(cleaned) > 90) and not has_new_course_structure:
        return True
    if _looks_like_course_title(cleaned):
        return False
    if cleaned[0].islower() or ACTION_SENTENCE_START_RE.search(_strip_accents(cleaned)):
        return True
    previous = blocks[-1][-1].strip()
    if previous.endswith((":", ",", ";")):
        return True
    if WORKLOAD_TOKEN_RE.search(cleaned) or COURSE_DATE_TOKEN_RE.search(cleaned):
        leftover = WORKLOAD_TOKEN_RE.sub("", cleaned)
        leftover = COURSE_DATE_TOKEN_RE.sub("", leftover).strip(" ·•-–—,;")
        # Pure metadata (`40 horas · Out/2026`) extends the previous course;
        # a title carrying its own metadata starts a new one.
        return not leftover or len(leftover) <= 2
    if _is_course_institution_candidate(cleaned):
        return True
    words = cleaned.split()
    capitalized = sum(1 for word in words if word[:1].isupper())
    if len(words) >= 3 and capitalized == 1:
        normalized_words = {_strip_accents(word).casefold().strip("(),.") for word in words}
        strong_cues = normalized_words & (
            _COURSE_DESCRIPTION_WORDS
            - {"formacao", "projetos", "dados", "online", "remoto", "pratico", "avancado", "basico"}
        )
        if strong_cues:
            return True
        if len(cleaned) <= 45 and len(words) <= 5 and not blocks[-1][1:]:
            # Short noun phrase such as `Gestão de projetos`: a distinct course, not prose.
            return False
        return True
    return len(words) >= 6


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
    institution = ""

    # Check second line if it holds institution / workload / date
    remaining_desc: list[str] = []
    if inline_desc:
        remaining_desc.append(inline_desc)

    for seg in segs[1:]:
        if not institution and _is_course_institution_candidate(seg):
            institution = seg
        else:
            remaining_desc.append(seg)

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
            if leftover_inst and not institution and _is_course_institution_candidate(leftover_inst):
                institution = leftover_inst
            elif leftover_inst:
                remaining_desc.append(leftover_inst)
        elif not institution and _is_course_institution_candidate(sub):
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
            if blocks and _is_course_continuation(s, blocks):
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


def _split_skills_outside_parentheses(text: str) -> list[str]:
    """Splits skill lists only at top level, keeping nested groups such as
    `Java (POO, Collections)` intact."""
    tokens: list[str] = []
    buffer: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in "([{":
            depth += 1
            buffer.append(char)
        elif char in ")]}":
            depth = max(0, depth - 1)
            buffer.append(char)
        elif depth == 0 and char in ",;•·|":
            tokens.append("".join(buffer))
            buffer = []
        elif depth == 0 and text.startswith(" e ", index) and index + 3 < len(text) and text[index + 3].isupper():
            tokens.append("".join(buffer))
            buffer = []
            index += 3
            continue
        else:
            buffer.append(char)
        index += 1
    tokens.append("".join(buffer))
    return [re.sub(r"\s+", " ", token).strip(" .") for token in tokens if token.strip(" .")]


def _line_has_contact(line: str) -> bool:
    return bool("@" in line or OUTBOUND_PHONE_RE.search(line) or OUTBOUND_URL_RE.search(line))


def heuristic_parse_resume_text(raw_text: str, embedded_urls: list[str]) -> MasterProfile:
    raw_text = unicodedata.normalize("NFKC", raw_text)
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
            # Location may live on a mixed contact line, so the full preamble is
            # collected here and filtered separately before building the summary.
            preamble_lines.append(ln)
        else:
            buckets[current_bucket].append(ln)

    city, state = _extract_city_state("\n".join(preamble_lines))

    summary_lines = [s.lstrip("•*-– ").strip() for s in buckets["summary"] if s.strip()]
    summary_text = "\n".join(summary_lines).strip()
    if not summary_text and len(preamble_lines) >= 1:
        long_preamble = [p for p in preamble_lines if len(p) > 65 and not _line_has_contact(p)]
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
        for token in _split_skills_outside_parentheses(cleaned_sk):
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

    raw_text = unicodedata.normalize("NFKC", raw_text).strip()
    if len(raw_text) < 20:
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
