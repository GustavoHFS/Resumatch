"""Editable Portuguese-BR A4 `ResumeDraft` v2.1 contract with `font_family`, split course fields, and clean projects."""

from __future__ import annotations

from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from app.models.profile_schema import (
    CandidateHeader,
    ProfileLink,
    ResumeFontFamily,
    SectionKind,
    SectionLabels,
    StrictModel,
)


def _draft_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


class DraftHeader(StrictModel):
    full_name: str = Field(min_length=2, max_length=140)
    location_line: str = Field(default="", max_length=200)
    contact_line: str = Field(default="", max_length=220)
    email: str = Field(default="", max_length=150)
    phone: str = Field(default="", max_length=50)
    links: list[ProfileLink] = Field(default_factory=list)


def build_draft_header_from_profile(header: CandidateHeader) -> DraftHeader:
    loc_parts: list[str] = []
    if header.neighborhood:
        loc_parts.append(header.neighborhood)
    if header.city and header.state:
        loc_parts.append(
            f"{header.city} - {header.state}" if len(header.state) == 2 else f"{header.city}, {header.state}"
        )
    elif header.city:
        loc_parts.append(header.city)
    if header.country and loc_parts:
        loc_parts.append(header.country)

    contact_parts = [p for p in (header.phone, header.email) if p.strip()]
    return DraftHeader(
        full_name=header.full_name,
        location_line=" · ".join(loc_parts),
        contact_line=" · ".join(contact_parts),
        email=header.email,
        phone=header.phone,
        links=list(header.links),
    )


class DraftExperienceItem(StrictModel):
    id: str = Field(default_factory=lambda: _draft_id("dexp"), min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=160)
    company: str = Field(default="", max_length=160)
    period: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4000)
    highlights: list[str] = Field(default_factory=list)

    @field_validator("highlights")
    @classmethod
    def strip_empty_highlights(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class DraftEducationItem(StrictModel):
    id: str = Field(default_factory=lambda: _draft_id("dedu"), min_length=1, max_length=80)
    degree: str = Field(min_length=1, max_length=180)
    institution: str = Field(default="", max_length=180)
    period: str = Field(default="", max_length=140)
    details: str = Field(default="", max_length=600)


class DraftCourseItem(StrictModel):
    id: str = Field(default_factory=lambda: _draft_id("dcrs"), min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=180)
    institution: str = Field(default="", max_length=160)
    workload: str = Field(default="", max_length=80)
    completion_date: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=1000)


class DraftProjectItem(StrictModel):
    id: str = Field(default_factory=lambda: _draft_id("dprj"), min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    url: str | None = Field(default=None, max_length=300)
    description: str = Field(default="", max_length=2000)
    highlights: list[str] = Field(default_factory=list)

    @field_validator("highlights")
    @classmethod
    def strip_empty_project_highlights(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class DraftSkillGroup(StrictModel):
    id: str = Field(default_factory=lambda: _draft_id("dsk"), min_length=1, max_length=80)
    label: str = Field(default="", max_length=90)
    items: list[str] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def strip_empty_skill_items(cls, value: list[str]) -> list[str]:
        return [s.strip() for s in value if s and s.strip()]


class DraftLanguageItem(StrictModel):
    id: str = Field(default_factory=lambda: _draft_id("dlng"), min_length=1, max_length=80)
    language: str = Field(min_length=1, max_length=80)
    proficiency: str = Field(default="", max_length=80)


DEFAULT_SECTION_ORDER: list[SectionKind] = [
    "summary",
    "experience",
    "education",
    "projects",
    "courses",
    "skills",
    "languages",
]


class ResumeDraft(StrictModel):
    target_role: str = Field(default="", max_length=200)
    company: str | None = Field(default=None, max_length=180)
    max_pages: int = Field(default=1, ge=1, le=2)
    font_family: ResumeFontFamily = "Libre Franklin"
    compact_mode: bool = False

    @field_validator("font_family", mode="before")
    @classmethod
    def migrate_legacy_font_family(cls, value: object) -> object:
        if value == "Merriweather":
            return "Times New Roman"
        return value
    section_labels: SectionLabels = Field(default_factory=SectionLabels)
    section_order: list[SectionKind] = Field(default_factory=lambda: list(DEFAULT_SECTION_ORDER))
    page_break_before_sections: list[SectionKind] = Field(default_factory=list)

    header: DraftHeader
    summary: str = Field(default="", max_length=3000)
    experiences: list[DraftExperienceItem] = Field(default_factory=list)
    education: list[DraftEducationItem] = Field(default_factory=list)
    courses: list[DraftCourseItem] = Field(default_factory=list)
    projects: list[DraftProjectItem] = Field(default_factory=list)
    skill_groups: list[DraftSkillGroup] = Field(default_factory=list)
    languages: list[DraftLanguageItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_unique_section_order(self) -> ResumeDraft:
        dedup_order: list[SectionKind] = []
        for kind in self.section_order:
            if kind not in dedup_order:
                dedup_order.append(kind)
        self.section_order = dedup_order or list(DEFAULT_SECTION_ORDER)
        return self
