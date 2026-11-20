"""Reversible PII Stripper & Rehydrator for Resumatch v2.0 AI workflows.

Protects two outbound LLM flows:
1. `strip_pii_for_resume_generation`:
   - Never transmits `CandidateHeader` (`full_name`, `email`, `phone`, `neighborhood`, `links`)
     or `BlockedCompaniesConfig`.
   - Masks employer names (`[[EMP_*]]`), academic/course institutions (`[[INST_*]]`), and any
     emails, phone numbers, or URLs inside text fields before outbound serialization.
   - Rehydrates placeholders locally into an editable `ResumeDraft` with the real `DraftHeader`.
2. `strip_pii_for_profile_import`:
   - Masks emails (`[[EMAIL_*]]`), phone numbers (`[[PHONE_*]]`), and URLs (`[[URL_*]]`) from
     raw `.pdf`/`.docx` resume text before LLM structuring, restoring them locally afterward.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from app.models.blocked_schema import BlockedCompaniesConfig
from app.models.profile_schema import MasterProfile, StrictModel
from app.models.resume_schema import (
    DraftCourseItem,
    DraftEducationItem,
    DraftExperienceItem,
    DraftLanguageItem,
    DraftProjectItem,
    DraftSkillGroup,
    ResumeDraft,
    build_draft_header_from_profile,
)

OUTBOUND_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
OUTBOUND_PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[\s.-]*)?(?:\([0-9]{2,3}\)|(?<!\d)[1-9][0-9])[\s.-]*9?[0-9]{4}[-\s][0-9]{4}\b"
)
OUTBOUND_URL_RE = re.compile(
    r"https?://[^\s,;)>\]]+|(?:www\.|(?:github|linkedin)\.com/)[^\s,;)>\]]+",
    re.IGNORECASE,
)


class SanitizedExperienceInput(StrictModel):
    id: str
    role: str
    company_token: str
    period: str
    description: str


class SanitizedEducationInput(StrictModel):
    id: str
    degree: str
    institution_token: str
    period: str
    details: str


class SanitizedCourseInput(StrictModel):
    id: str
    title: str
    institution_token: str
    workload: str
    completion_date: str
    description: str


class SanitizedProjectInput(StrictModel):
    id: str
    name: str
    url_token: str | None = None
    description: str


class SanitizedLanguageInput(StrictModel):
    id: str
    language: str
    proficiency: str


class SanitizedResumeGenInput(StrictModel):
    max_pages: int = Field(default=1, ge=1, le=2)
    target_job_title: str = ""
    target_job_description: str = ""
    base_summary: str = ""
    experiences: list[SanitizedExperienceInput] = Field(default_factory=list)
    education: list[SanitizedEducationInput] = Field(default_factory=list)
    courses: list[SanitizedCourseInput] = Field(default_factory=list)
    projects: list[SanitizedProjectInput] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    languages: list[SanitizedLanguageInput] = Field(default_factory=list)


@dataclass(frozen=True)
class ResumeGenStripContext:
    sanitized: SanitizedResumeGenInput
    placeholder_to_original: dict[str, str]


def _collect_named_masks(
    profile: MasterProfile,
    blocked: BlockedCompaniesConfig,
    target_company: str | None = None,
) -> list[tuple[str, str]]:
    institutions: list[str] = []
    for edu in profile.education:
        if edu.institution:
            institutions.append(edu.institution)
            if "—" in edu.institution:
                institutions.extend(p.strip() for p in edu.institution.split("—", 1))
            if "(" in edu.institution:
                institutions.append(edu.institution.split("(", 1)[0].strip())
                institutions.extend(re.findall(r"\(([^)]+)\)", edu.institution))
        if edu.details:
            cleaned_det = re.sub(r"^(?:em andamento\s*·\s*)?via\s+", "", edu.details, flags=re.I).strip()
            if len(cleaned_det) >= 4:
                institutions.append(cleaned_det)

    for crs in profile.courses:
        if crs.institution and crs.institution.casefold() not in {"vivência prática", "vivencia pratica"}:
            institutions.append(crs.institution)

    employers: list[str] = [exp.company for exp in profile.experiences if exp.company]
    if target_company and target_company.strip():
        employers.append(target_company.strip())
    for blk in blocked.companies:
        employers.append(blk.display_name)
        if blk.corporate_name:
            employers.append(blk.corporate_name)
        employers.extend(blk.aliases)

    personal_tokens: list[str] = [profile.header.full_name]
    if profile.header.neighborhood:
        personal_tokens.append(profile.header.neighborhood)

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _register(items: list[str], prefix: str, min_len: int = 3) -> None:
        idx = 1
        for raw in sorted({x.strip() for x in items if x and len(x.strip()) >= min_len}, key=len, reverse=True):
            low = raw.casefold()
            if low in seen:
                continue
            seen.add(low)
            pairs.append((raw, f"[[{prefix}_{idx}]]"))
            idx += 1

    _register(personal_tokens, "CANDIDATE", min_len=4)
    _register(institutions, "INST", min_len=3)
    _register(employers, "EMP", min_len=3)
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


class _MaskerState:
    def __init__(self, named_masks: list[tuple[str, str]]) -> None:
        self.named_masks = named_masks
        self.placeholder_to_original: dict[str, str] = {}
        self._original_to_placeholder: dict[str, str] = {}
        self._counters: dict[str, int] = {"EMAIL": 0, "PHONE": 0, "URL": 0}

    def _token_for_regex_hit(self, prefix: str, raw_hit: str) -> str:
        key = raw_hit.strip()
        if key in self._original_to_placeholder:
            return self._original_to_placeholder[key]
        self._counters[prefix] += 1
        tok = f"[[{prefix}_{self._counters[prefix]}]]"
        self._original_to_placeholder[key] = tok
        self.placeholder_to_original[tok] = key
        return tok

    def mask_text(self, value: str) -> str:
        if not value:
            return ""
        out = value
        # 1. Mask URLs, emails, and phones first
        out = OUTBOUND_URL_RE.sub(lambda m: self._token_for_regex_hit("URL", m.group(0)), out)
        out = OUTBOUND_EMAIL_RE.sub(lambda m: self._token_for_regex_hit("EMAIL", m.group(0)), out)
        out = OUTBOUND_PHONE_RE.sub(lambda m: self._token_for_regex_hit("PHONE", m.group(0)), out)

        # 2. Mask named entities (candidate name, neighborhood, institutions, employers)
        for original, tok in self.named_masks:
            pattern = re.compile(r"(?<!\w)" + re.escape(original) + r"(?!\w)", re.IGNORECASE)
            if pattern.search(out):
                out = pattern.sub(tok, out)
                self.placeholder_to_original[tok] = original
        return out


def rehydrate_text(value: str, placeholder_to_original: dict[str, str]) -> str:
    if not value:
        return ""
    restored = value
    for tok, original in placeholder_to_original.items():
        restored = restored.replace(tok, original)
    return restored


def strip_pii_for_resume_generation(
    *,
    profile: MasterProfile,
    blocked: BlockedCompaniesConfig,
    job_title: str = "",
    company: str | None = None,
    job_description: str = "",
    max_pages: int = 1,
) -> ResumeGenStripContext:
    named_masks = _collect_named_masks(profile, blocked, target_company=company)
    masker = _MaskerState(named_masks)

    sanitized = SanitizedResumeGenInput(
        max_pages=max_pages,
        target_job_title=masker.mask_text(job_title),
        target_job_description=masker.mask_text(job_description),
        base_summary=masker.mask_text(profile.summary),
        experiences=[
            SanitizedExperienceInput(
                id=exp.id,
                role=masker.mask_text(exp.role),
                company_token=masker.mask_text(exp.company),
                period=masker.mask_text(exp.period),
                description=masker.mask_text(exp.description),
            )
            for exp in profile.experiences
        ],
        education=[
            SanitizedEducationInput(
                id=edu.id,
                degree=masker.mask_text(edu.degree),
                institution_token=masker.mask_text(edu.institution),
                period=masker.mask_text(edu.period),
                details="",
            )
            for edu in profile.education
        ],
        courses=[
            SanitizedCourseInput(
                id=crs.id,
                title=masker.mask_text(crs.title),
                institution_token=masker.mask_text(crs.institution),
                workload=masker.mask_text(crs.workload),
                completion_date=masker.mask_text(crs.completion_date),
                description=masker.mask_text(crs.description),
            )
            for crs in profile.courses
        ],
        projects=[
            SanitizedProjectInput(
                id=prj.id,
                name=masker.mask_text(prj.name),
                url_token=masker.mask_text(prj.url) if prj.url else None,
                description=masker.mask_text(prj.description),
            )
            for prj in profile.projects
        ],
        skills=[masker.mask_text(sk) for sk in profile.skills],
        languages=[
            SanitizedLanguageInput(
                id=lng.id,
                language=lng.language,
                proficiency=lng.proficiency,
            )
            for lng in profile.languages
        ],
    )

    serialized = json.dumps(sanitized.model_dump(mode="json"), ensure_ascii=False)
    sensitive_checks = [
        profile.header.full_name,
        profile.header.email,
        profile.header.phone,
        *([profile.header.neighborhood] if profile.header.neighborhood else []),
        *(lnk.display_text for lnk in profile.header.links),
        *(blk.display_name for blk in blocked.companies),
    ]
    for pii_val in sensitive_checks:
        cleaned_pii = (pii_val or "").strip()
        if len(cleaned_pii) >= 3:
            boundary_re = re.compile(r"(?<!\w)" + re.escape(cleaned_pii) + r"(?!\w)", re.IGNORECASE)
            if boundary_re.search(serialized):
                raise ValueError(
                    "PIIStripper safety halt: personal or blocked identifier detected in outbound payload."
                )

    if (
        OUTBOUND_EMAIL_RE.search(serialized)
        or OUTBOUND_PHONE_RE.search(serialized)
        or OUTBOUND_URL_RE.search(serialized)
    ):
        raise ValueError("PIIStripper safety halt: email, phone, or URL detected in outbound payload.")

    return ResumeGenStripContext(
        sanitized=sanitized,
        placeholder_to_original=masker.placeholder_to_original,
    )


def rehydrate_generated_draft(
    *,
    proposal_dict: dict[str, Any],
    profile: MasterProfile,
    max_pages: int,
    company: str | None,
    placeholder_to_original: dict[str, str],
) -> ResumeDraft:
    def _rh(val: str | None) -> str:
        return rehydrate_text(val or "", placeholder_to_original)

    exp_loc_by_id = {exp.id: exp.location for exp in profile.experiences}

    experiences = [
        DraftExperienceItem(
            id=str(item.get("id") or f"dexp_{idx}"),
            role=_rh(item.get("role")) or "Experiência",
            company=_rh(item.get("company")),
            period=_rh(item.get("period")),
            location=_rh(item.get("location")) or exp_loc_by_id.get(str(item.get("id")), ""),
            description=_rh(item.get("description")),
            highlights=[_rh(h) for h in (item.get("highlights") or []) if _rh(h)],
        )
        for idx, item in enumerate(proposal_dict.get("experiences") or [], start=1)
        if isinstance(item, dict) and (_rh(item.get("role")) or _rh(item.get("company")))
    ]

    # Academic facts and order belong to the candidate, never to the model.
    education = [
        DraftEducationItem(
            id=item.id, degree=item.degree, institution=item.institution,
            period=item.period,
        )
        for item in profile.education
    ]

    courses = [
        DraftCourseItem(
            id=str(item.get("id") or f"dcrs_{idx}"),
            title=_rh(item.get("title")) or "Curso",
            institution=_rh(item.get("institution")),
            workload=_rh(item.get("workload")),
            completion_date=_rh(item.get("completion_date")),
            description=_rh(item.get("description")),
        )
        for idx, item in enumerate(proposal_dict.get("courses") or [], start=1)
        if isinstance(item, dict) and _rh(item.get("title"))
    ]

    projects: list[DraftProjectItem] = []
    for idx, item in enumerate(proposal_dict.get("projects") or [], start=1):
        if not isinstance(item, dict) or not _rh(item.get("name")):
            continue
        raw_url = _rh(item.get("url"))
        clean_url = raw_url if raw_url.lower().startswith("https://") else None
        projects.append(
            DraftProjectItem(
                id=str(item.get("id") or f"dprj_{idx}"),
                name=_rh(item.get("name")),
                url=clean_url,
                description=_rh(item.get("description")),
                highlights=[_rh(h) for h in (item.get("highlights") or []) if _rh(h)],
            )
        )

    skill_groups = [
        DraftSkillGroup(
            id=str(item.get("id") or f"dsk_{idx}"),
            label=_rh(item.get("label")),
            items=[_rh(s) for s in (item.get("items") or []) if _rh(s)],
        )
        for idx, item in enumerate(proposal_dict.get("skill_groups") or [], start=1)
        if isinstance(item, dict) and any(_rh(s) for s in (item.get("items") or []))
    ]

    languages = [
        DraftLanguageItem(
            id=str(item.get("id") or f"dlng_{idx}"),
            language=_rh(item.get("language")) or "Idioma",
            proficiency=_rh(item.get("proficiency")),
        )
        for idx, item in enumerate(proposal_dict.get("languages") or [], start=1)
        if isinstance(item, dict) and _rh(item.get("language"))
    ]

    valid_sections = {"summary", "experience", "education", "projects", "courses", "skills", "languages"}
    raw_order = [s for s in (proposal_dict.get("section_order") or []) if s in valid_sections]

    return ResumeDraft(
        target_role=_rh(proposal_dict.get("target_role")),
        company=company,
        max_pages=max_pages,
        header=build_draft_header_from_profile(profile.header),
        section_order=raw_order or [
            "summary",
            "experience",
            "education",
            "projects",
            "courses",
            "skills",
            "languages",
        ],  # type: ignore[arg-type]
        summary=_rh(proposal_dict.get("summary")),
        experiences=experiences,
        education=education,
        courses=courses,
        projects=projects,
        skill_groups=skill_groups,
        languages=languages,
    )


def strip_pii_for_profile_import(raw_text: str) -> tuple[str, dict[str, str]]:
    """Masks emails, phone numbers, and URLs before sending uploaded resume text to an external LLM."""
    masker = _MaskerState(named_masks=[])
    masked = masker.mask_text(raw_text)
    return masked, masker.placeholder_to_original
