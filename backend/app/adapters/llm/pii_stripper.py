"""Minimal-token PII Stripper for single-call Surgical AI ATS fine-tuning.

Security & Privacy Invariants:
- NEVER transmits `CandidateHeader` (name, email, phone, neighborhood, city, links).
- NEVER transmits `BlockedCompaniesConfig`.
- Masks all employer names (`experiences[].company`) and academic/course institution
  names (`education[].institution`, `courses[].institution`, acronyms) in `summary_text`
  and `bullets` using reversible placeholders (`[[INST_1]]`, `[[EMP_1]]`) before outbound
  serialization, and rehydrates them locally upon return.
- Sends ONLY `{locale, summary_text, bullets: [{bullet_id, text}], confirmed_job_keywords}`.
- Asserts zero email addresses, phone numbers, or URLs remain in the outbound payload.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import Field

from app.domain.resume_composer import ResumeDocumentPayload
from app.domain.triage_engine import TriageResult
from app.models import BlockedCompaniesConfig, MasterProfile
from app.models.profile_schema import StrictModel, SupportedLocale

OUTBOUND_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
OUTBOUND_PHONE_RE = re.compile(
    r"(?:\+55\s*)?(?:\([1-9]{2}\)|(?<!\d)[1-9]{2})\s*9?[1-9]\d{3}[-\s]?\d{4}\b"
)
OUTBOUND_URL_RE = re.compile(r"https?://\S+|(?:github|linkedin)\.com/\S+", re.IGNORECASE)


class SanitizedBulletItem(StrictModel):
    bullet_id: str
    text: str


class SanitizedSurgicalInput(StrictModel):
    locale: SupportedLocale
    summary_text: str
    bullets: list[SanitizedBulletItem]
    confirmed_job_keywords: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class StripContext:
    sanitized: SanitizedSurgicalInput
    placeholder_to_original: dict[str, str]


def _collect_entity_masks(
    profile: MasterProfile,
    blocked: BlockedCompaniesConfig,
) -> list[tuple[str, str]]:
    """Builds ordered `(sensitive_term, placeholder)` pairs (longest first) for masking."""
    raw_institutions: list[str] = []
    for edu in profile.education:
        raw_institutions.append(edu.institution)
        if "—" in edu.institution:
            acronym, full = [p.strip() for p in edu.institution.split("—", 1)]
            raw_institutions.extend([acronym, full])
        if "(" in edu.institution:
            before_paren = edu.institution.split("(", 1)[0].strip()
            raw_institutions.append(before_paren)
            inside = re.findall(r"\(([^)]+)\)", edu.institution)
            raw_institutions.extend(inside)
        if edu.program_note:
            raw_institutions.append(edu.program_note.replace("via ", "").strip())
        for tr in edu.translations.values():
            raw_institutions.append(tr.institution)

    for course in profile.courses:
        if course.entry_type == "course":
            raw_institutions.append(course.institution)

    raw_employers: list[str] = [exp.company for exp in profile.experiences]
    for company in blocked.companies:
        raw_employers.append(company.display_name)
        if company.corporate_name:
            raw_employers.append(company.corporate_name)
        raw_employers.extend(company.aliases)

    pairs: list[tuple[str, str]] = []
    seen_lower: set[str] = set()
    inst_idx = 1
    for term in sorted({t.strip() for t in raw_institutions if len(t.strip()) >= 3}, key=len, reverse=True):
        low = term.casefold()
        if low in seen_lower:
            continue
        seen_lower.add(low)
        pairs.append((term, f"[[INST_{inst_idx}]]"))
        inst_idx += 1

    emp_idx = 1
    for term in sorted({t.strip() for t in raw_employers if len(t.strip()) >= 3}, key=len, reverse=True):
        low = term.casefold()
        if low in seen_lower:
            continue
        seen_lower.add(low)
        pairs.append((term, f"[[EMP_{emp_idx}]]"))
        emp_idx += 1

    # Sort all pairs longest-first so full names replace before acronyms
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def _apply_masks(text: str, masks: list[tuple[str, str]]) -> tuple[str, dict[str, str]]:
    masked = text
    used: dict[str, str] = {}
    for original, token in masks:
        pattern = re.compile(r"(?<!\w)" + re.escape(original) + r"(?!\w)", re.IGNORECASE)
        if pattern.search(masked):
            masked = pattern.sub(token, masked)
            used[token] = original
    return masked, used


def rehydrate_placeholders(text: str, placeholder_to_original: dict[str, str]) -> str:
    restored = text
    for token, original in placeholder_to_original.items():
        restored = restored.replace(token, original)
    return restored


def strip_pii_for_surgical_llm(
    payload: ResumeDocumentPayload,
    profile: MasterProfile,
    blocked: BlockedCompaniesConfig,
    triage: TriageResult | None = None,
    extra_job_keywords: list[str] | None = None,
) -> StripContext:
    """Produces a minimal-token, zero-PII input payload for single-call ATS fine-tuning."""
    masks = _collect_entity_masks(profile, blocked)
    placeholder_map: dict[str, str] = {}

    masked_summary, used_sum = _apply_masks(payload.summary.text, masks)
    placeholder_map.update(used_sum)

    sanitized_bullets: list[SanitizedBulletItem] = []
    for exp in payload.experiences:
        for bullet in exp.bullets:
            m_txt, used_b = _apply_masks(bullet.text, masks)
            placeholder_map.update(used_b)
            sanitized_bullets.append(SanitizedBulletItem(bullet_id=bullet.id, text=m_txt))

    # Only confirmed/academic resume-eligible keywords may be suggested to the LLM!
    allowed_skill_aliases = {
        alias.casefold()
        for sk in profile.skills
        if sk.can_appear_on_resume
        for alias in [sk.display_name, *sk.aliases]
        if alias.strip()
    }
    confirmed_keywords: list[str] = []
    if triage is not None:
        for req in triage.requirements:
            if req.can_appear_on_resume:
                for kw in req.matched_keywords:
                    if kw not in confirmed_keywords:
                        confirmed_keywords.append(kw)
    for kw in extra_job_keywords or []:
        if kw.strip().casefold() in allowed_skill_aliases and kw not in confirmed_keywords:
            confirmed_keywords.append(kw.strip())

    sanitized = SanitizedSurgicalInput(
        locale=payload.locale,
        summary_text=masked_summary,
        bullets=sanitized_bullets,
        confirmed_job_keywords=confirmed_keywords[:20],
    )

    # Final hard assertion: zero candidate PII, emails, phones, links, or unmasked blocked companies
    serialized = json.dumps(sanitized.model_dump(mode="json"), ensure_ascii=False)
    forbidden_pii = [
        profile.header.full_name,
        profile.header.email,
        profile.header.phone,
        *( [profile.header.neighborhood] if profile.header.neighborhood else [] ),
        *(lnk.display_text for lnk in profile.header.links),
        *(c.display_name for c in blocked.companies),
        *(exp.company for exp in profile.experiences),
    ]
    for pii_val in forbidden_pii:
        if pii_val and len(pii_val.strip()) >= 4 and pii_val.casefold() in serialized.casefold():
            raise ValueError("PIIStripper safety halt: residual personal/company identifier detected.")

    if OUTBOUND_EMAIL_RE.search(serialized) or OUTBOUND_PHONE_RE.search(serialized) or OUTBOUND_URL_RE.search(serialized):
        raise ValueError("PIIStripper safety halt: email, phone, or URL detected in outbound payload.")

    return StripContext(sanitized=sanitized, placeholder_to_original=placeholder_map)
