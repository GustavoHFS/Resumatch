"""Automated post-render ATS PDF validator powered by `pypdf`.

Verifies:
- Page count strictly `<= payload.max_pages` (and exact 2 pages when `max_pages == 2`
  with explicit page break).
- A4 paper geometry (`595.28 x 841.89 pt` within tolerance).
- Clean linear text extraction in exact DOM section order.
- Clickable URI link annotations matching `payload.header.links` (GitHub / LinkedIn).
- Complete absence of `forbidden_terms_checked` and blocked `preparable_gap` skills.
"""

from __future__ import annotations

from io import BytesIO
from typing import Iterable

from pydantic import Field
from pypdf import PdfReader

from app.domain.resume_composer import ResumeDocumentPayload
from app.domain.triage_engine import contains_term, normalize_text
from app.models.profile_schema import StrictModel


class AtsValidationReport(StrictModel):
    passed: bool
    page_count: int
    max_pages: int
    a4_dimensions_ok: bool
    candidate_name_present: bool
    section_headings_in_order: bool
    clickable_links_valid: bool
    zero_forbidden_terms: bool
    clickable_uris: list[str] = Field(default_factory=list)
    missing_links: list[str] = Field(default_factory=list)
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


def _expected_headings(payload: ResumeDocumentPayload) -> list[str]:
    labels = payload.section_labels
    mapping = {
        "summary": (True, labels.summary),
        "education": (bool(payload.education), labels.education),
        "experience": (bool(payload.experiences), labels.experience),
        "courses": (bool(payload.courses), labels.courses),
        "projects": (bool(payload.projects), labels.projects),
        "skills": (bool(payload.skill_groups), labels.skills),
        "languages": (bool(payload.languages), labels.languages),
    }
    headings: list[str] = []
    for kind in payload.section_order:
        present, title = mapping[kind]
        if present:
            headings.append(title)
    return headings


def validate_ats_pdf(
    pdf_bytes: bytes,
    payload: ResumeDocumentPayload,
    forbidden_terms: Iterable[str] = (),
) -> AtsValidationReport:
    reader = PdfReader(BytesIO(pdf_bytes))
    page_count = len(reader.pages)
    if payload.max_pages == 2 and payload.page_break_before_sections:
        pages_ok = page_count == 2
    else:
        pages_ok = 1 <= page_count <= payload.max_pages

    a4_ok = bool(reader.pages) and all(
        abs(float(p.mediabox.width) - 595.28) < 3.5
        and abs(float(p.mediabox.height) - 841.89) < 3.5
        for p in reader.pages
    )

    extracted_text = "\n".join((p.extract_text() or "") for p in reader.pages)
    norm_extracted = normalize_text(extracted_text)

    name_ok = normalize_text(payload.header.full_name) in norm_extracted

    # Verify section headings appear in strictly increasing linear order
    headings = _expected_headings(payload)
    cursor = 0
    order_ok = True
    for heading in headings:
        norm_h = normalize_text(heading)
        pos = norm_extracted.find(norm_h, cursor)
        if pos == -1:
            order_ok = False
            break
        cursor = pos + len(norm_h)

    found_uris = _extract_pdf_uris(reader)
    found_uri_set = {u.rstrip("/") for u in found_uris}
    expected_links = [lnk.url.rstrip("/") for lnk in payload.header.links]
    missing_links = [u for u in expected_links if u not in found_uri_set]
    links_ok = len(missing_links) == 0

    forbidden_hits: list[str] = []
    for term in forbidden_terms:
        if term and contains_term(extracted_text, term):
            forbidden_hits.append(term)
    zero_forbidden = len(forbidden_hits) == 0

    checks = {
        "page_count_within_limit": pages_ok,
        "a4_geometry": a4_ok,
        "candidate_name_present": name_ok,
        "section_headings_linear_order": order_ok,
        "clickable_links_preserved": links_ok,
        "zero_forbidden_or_unverified_terms": zero_forbidden,
    }

    return AtsValidationReport(
        passed=all(checks.values()),
        page_count=page_count,
        max_pages=payload.max_pages,
        a4_dimensions_ok=a4_ok,
        candidate_name_present=name_ok,
        section_headings_in_order=order_ok,
        clickable_links_valid=links_ok,
        zero_forbidden_terms=zero_forbidden,
        clickable_uris=sorted(set(found_uris)),
        missing_links=missing_links,
        forbidden_hits=sorted(set(forbidden_hits)),
        extracted_char_count=len(extracted_text.strip()),
        checks=checks,
    )
