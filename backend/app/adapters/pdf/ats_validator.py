"""Automated post-render ATS PDF validator powered by `pypdf`.

Verifies:
- Page count strictly `<= draft.max_pages`.
- A4 paper geometry (`595.28 x 841.89 pt` within tolerance).
- Zero corrupted OpenType ligatures (`ﬀ`, `ﬁ`, `ﬂ`, `ﬃ`, `ﬄ`) or dotless `ı` (`U+0131`).
- Clean linear text extraction in exact DOM section order (with whitespace normalization).
- Clickable URI link annotations matching `draft.header.links`.
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Iterable

from pydantic import Field
from pypdf import PdfReader

from app.domain.triage_engine import contains_term, normalize_text
from app.models.profile_schema import SectionLabels, StrictModel
from app.models.resume_schema import ResumeDraft

LIGATURE_OR_DOTLESS_RE = re.compile(r"[\ufb00-\ufb06\u0131]")


class AtsValidationReport(StrictModel):
    passed: bool
    page_count: int
    max_pages: int
    a4_dimensions_ok: bool
    candidate_name_present: bool
    section_headings_in_order: bool
    clickable_links_valid: bool
    zero_corrupted_ligatures: bool = True
    zero_forbidden_terms: bool
    clickable_uris: list[str] = Field(default_factory=list)
    missing_links: list[str] = Field(default_factory=list)
    corrupted_glyphs: list[str] = Field(default_factory=list)
    forbidden_hits: list[str] = Field(default_factory=list)
    extracted_char_count: int = 0
    checks: dict[str, bool] = Field(default_factory=dict)


def _extract_pdf_uris(reader: PdfReader) -> list[str]:
    uris: list[str] = []
    for page in reader.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        annot_list = annots.get_object() if hasattr(annots, "get_object") else annots
        if not isinstance(annot_list, Iterable):
            continue
        for annot_ref in annot_list:
            annot = annot_ref.get_object() if hasattr(annot_ref, "get_object") else annot_ref
            if not isinstance(annot, dict) or annot.get("/Subtype") != "/Link":
                continue
            action = annot.get("/A")
            action_obj = action.get_object() if hasattr(action, "get_object") else action
            if isinstance(action_obj, dict) and action_obj.get("/URI"):
                uris.append(str(action_obj["/URI"]))
    return uris


def _expected_headings(draft: ResumeDraft) -> list[str]:
    labels = draft.section_labels or SectionLabels()
    has_skills = any(bool(g.items) for g in draft.skill_groups)
    mapping = {
        "summary": (bool(draft.summary.strip()), labels.summary),
        "experience": (bool(draft.experiences), labels.experience),
        "education": (bool(draft.education), labels.education),
        "projects": (bool(draft.projects), labels.projects),
        "courses": (bool(draft.courses), labels.courses),
        "skills": (has_skills, labels.skills),
        "languages": (bool(draft.languages), labels.languages),
    }
    headings: list[str] = []
    for kind in draft.section_order:
        present, title = mapping[kind]
        if present:
            headings.append(title)
    return headings


def _collapse_spaces(value: str) -> str:
    return re.sub(r"[\t\r\f\v ]+", " ", value)


def validate_ats_pdf(
    pdf_bytes: bytes,
    draft: ResumeDraft,
    forbidden_terms: Iterable[str] = (),
) -> AtsValidationReport:
    reader = PdfReader(BytesIO(pdf_bytes))
    page_count = len(reader.pages)
    if draft.max_pages == 2 and draft.page_break_before_sections:
        pages_ok = page_count == 2
    else:
        pages_ok = 1 <= page_count <= draft.max_pages

    a4_ok = bool(reader.pages) and all(
        abs(float(p.mediabox.width) - 595.28) < 3.5
        and abs(float(p.mediabox.height) - 841.89) < 3.5
        for p in reader.pages
    )

    raw_extracted_text = "\n".join((p.extract_text() or "") for p in reader.pages)
    corrupted_hits = sorted(set(LIGATURE_OR_DOTLESS_RE.findall(raw_extracted_text)))
    ligatures_ok = len(corrupted_hits) == 0

    cleaned_extracted = _collapse_spaces(raw_extracted_text)
    norm_extracted = _collapse_spaces(normalize_text(cleaned_extracted))

    norm_name = _collapse_spaces(normalize_text(draft.header.full_name)).strip()
    name_ok = bool(norm_name) and norm_name in norm_extracted

    headings = _expected_headings(draft)
    cursor = 0
    order_ok = True
    for heading in headings:
        norm_h = _collapse_spaces(normalize_text(heading)).strip()
        pos = norm_extracted.find(norm_h, cursor)
        if pos == -1:
            order_ok = False
            break
        cursor = pos + len(norm_h)

    found_uris = _extract_pdf_uris(reader)
    found_uri_set = {u.rstrip("/") for u in found_uris}
    expected_links = [lnk.url.rstrip("/") for lnk in draft.header.links]
    missing_links = [u for u in expected_links if u not in found_uri_set]
    links_ok = len(missing_links) == 0

    forbidden_hits: list[str] = []
    for term in forbidden_terms:
        if term and contains_term(cleaned_extracted, term):
            forbidden_hits.append(term)
    zero_forbidden = len(forbidden_hits) == 0

    checks = {
        "page_count_within_limit": pages_ok,
        "a4_geometry": a4_ok,
        "candidate_name_present": name_ok,
        "section_headings_linear_order": order_ok,
        "clickable_links_preserved": links_ok,
        "zero_corrupted_ligatures": ligatures_ok,
        "zero_forbidden_or_unverified_terms": zero_forbidden,
    }

    return AtsValidationReport(
        passed=all(checks.values()),
        page_count=page_count,
        max_pages=draft.max_pages,
        a4_dimensions_ok=a4_ok,
        candidate_name_present=name_ok,
        section_headings_in_order=order_ok,
        clickable_links_valid=links_ok,
        zero_corrupted_ligatures=ligatures_ok,
        zero_forbidden_terms=zero_forbidden,
        clickable_uris=sorted(set(found_uris)),
        missing_links=missing_links,
        corrupted_glyphs=[f"U+{ord(ch):04X}" for ch in corrupted_hits],
        forbidden_hits=sorted(set(forbidden_hits)),
        extracted_char_count=len(cleaned_extracted.strip()),
        checks=checks,
    )
