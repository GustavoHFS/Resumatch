"""Pydantic v2 schemas for the candidate Master Profile and area presets.

Enforces strict domain veracity invariants at load time:
- Self-deprecating proficiency qualifiers (e.g., 'básico', 'apenas noções') are rejected on
  public-facing resume display fields.
- Skills marked as 'preparable_gap' or 'unconfirmed' can hold internal study plans for interview
  preparation, but can never be referenced in resume presets or summary slots.
- Free courses in progress ('in_progress_free') and unapproved projects ('reserved') are blocked
  from appearing in any rendered resume preset.
- Supports multi-locale resumes ('pt-BR' and 'en-US', such as 'suporte-en').
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SupportedLocale = Literal["pt-BR", "en-US"]
SectionKind = Literal[
    "summary",
    "education",
    "experience",
    "courses",
    "projects",
    "skills",
    "languages",
]
VeracityState = Literal[
    "confirmed_practice",
    "academic_foundation",
    "preparable_gap",
    "unconfirmed",
]
CourseStatus = Literal["completed", "in_progress_free", "planned"]
EducationStatus = Literal["completed", "in_progress"]
ProjectStatus = Literal["approved", "reserved"]

SELF_DEPRECATING_PATTERN = re.compile(
    r"(?<!visual\s)\b(?:"
    r"b[aá]sic[oa]s?|"
    r"n[ií]vel\s+inicial|"
    r"em\s+n[ií]vel\s+inicial|"
    r"apenas\s+no[cç][oõ]es|"
    r"no[cç][oõ]es\s+b[aá]sicas|"
    r"conhecimento\s+limitado|"
    r"familiaridade\s+b[aá]sica|"
    r"basic\s+level|"
    r"beginner\s+level|"
    r"limited\s+knowledge"
    r")\b",
    re.IGNORECASE,
)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
HTTPS_URL_PATTERN = re.compile(r"^https://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def assert_confident_presentation(value: str, field_label: str) -> str:
    """Rejects self-deprecating proficiency labels on candidate-facing resume text."""
    match = SELF_DEPRECATING_PATTERN.search(value)
    if match:
        raise ValueError(
            f"Self-deprecating qualifier '{match.group(0)}' is not allowed in {field_label}. "
            "Keep proficiency limits in internal_context and present concrete tools/activities."
        )
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProfileLink(StrictModel):
    label: str = Field(min_length=1, max_length=50)
    url: str = Field(min_length=10, max_length=300)
    display_text: str = Field(min_length=3, max_length=150)

    @field_validator("url")
    @classmethod
    def validate_https_url(cls, value: str) -> str:
        if not HTTPS_URL_PATTERN.match(value):
            raise ValueError("Profile link URL must use https://")
        return value


class CandidateHeader(StrictModel):
    full_name: str = Field(min_length=3, max_length=120)
    age: int | None = Field(default=None, ge=14, le=100)
    neighborhood: str | None = Field(default=None, max_length=80)
    city: str = Field(min_length=2, max_length=80)
    state: str = Field(min_length=2, max_length=80)
    country_by_locale: dict[SupportedLocale, str] = Field(
        default_factory=lambda: {"pt-BR": "Brasil", "en-US": "Brazil"}
    )
    email: str = Field(min_length=5, max_length=150)
    phone: str = Field(min_length=8, max_length=40)
    links: list[ProfileLink] = Field(default_factory=list, max_length=6)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not EMAIL_PATTERN.match(value):
            raise ValueError("Invalid email format")
        return value


class EducationTranslation(StrictModel):
    degree: str = Field(min_length=2, max_length=160)
    institution: str = Field(min_length=2, max_length=160)
    program_note: str | None = Field(default=None, max_length=120)
    period_label: str = Field(min_length=3, max_length=120)


class EducationEntry(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    degree: str = Field(min_length=2, max_length=160)
    institution: str = Field(min_length=2, max_length=160)
    program_note: str | None = Field(default=None, max_length=120)
    period_label: str = Field(min_length=3, max_length=120)
    status: EducationStatus
    areas: list[str] = Field(default_factory=list)
    factual_source: str = Field(min_length=3)
    translations: dict[SupportedLocale, EducationTranslation] = Field(default_factory=dict)


class BulletItem(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    text: str = Field(min_length=10, max_length=400)
    areas: list[str] = Field(min_length=1)
    skill_tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    factual_source: str = Field(min_length=3)
    translations: dict[SupportedLocale, str] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def validate_bullet_text(cls, value: str) -> str:
        return assert_confident_presentation(value, "bullet.text")

    @field_validator("translations")
    @classmethod
    def validate_bullet_translations(
        cls, value: dict[SupportedLocale, str]
    ) -> dict[SupportedLocale, str]:
        for locale, text in value.items():
            assert_confident_presentation(text, f"bullet.translations[{locale}]")
        return value


class ExperienceTranslation(StrictModel):
    role: str = Field(min_length=2, max_length=120)
    employment_type_label: str | None = Field(default=None, max_length=80)
    period_label: str = Field(min_length=3, max_length=80)


class ExperienceEntry(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    role: str = Field(min_length=2, max_length=120)
    company: str = Field(min_length=2, max_length=120)
    employment_type_label: str | None = Field(default=None, max_length=80)
    period_label: str = Field(min_length=3, max_length=80)
    bullets: list[BulletItem] = Field(min_length=1)
    factual_source: str = Field(min_length=3)
    translations: dict[SupportedLocale, ExperienceTranslation] = Field(default_factory=dict)


class CourseTranslation(StrictModel):
    title: str = Field(min_length=2, max_length=160)
    institution: str = Field(min_length=2, max_length=120)
    details_label: str = Field(min_length=2, max_length=120)


class CourseEntry(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    entry_type: Literal["course", "practical_assessment"] = "course"
    title: str = Field(min_length=2, max_length=160)
    institution: str = Field(min_length=2, max_length=120)
    details_label: str = Field(min_length=2, max_length=120)
    hours: int | None = Field(default=None, ge=1, le=2000)
    year: int | None = Field(default=None, ge=2000, le=2100)
    status: CourseStatus
    areas: list[str] = Field(default_factory=list)
    factual_source: str = Field(min_length=3)
    translations: dict[SupportedLocale, CourseTranslation] = Field(default_factory=dict)


class ProjectEntry(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    status: ProjectStatus
    summary: str | None = Field(default=None, max_length=350)
    bullets: list[BulletItem] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list)
    url: str | None = Field(default=None, max_length=300)
    factual_source: str = Field(min_length=3)


class SkillItem(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list)
    veracity: VeracityState
    internal_context: str | None = Field(default=None, max_length=300)
    summary_label: str | None = Field(default=None, max_length=120)
    study_plan: str | None = Field(default=None, max_length=500)
    factual_source: str | None = Field(default=None, max_length=300)
    translations: dict[SupportedLocale, str] = Field(default_factory=dict)

    @property
    def can_appear_on_resume(self) -> bool:
        return self.veracity in {"confirmed_practice", "academic_foundation"}

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return assert_confident_presentation(value, "skill.display_name")

    @field_validator("summary_label")
    @classmethod
    def validate_summary_label(cls, value: str | None) -> str | None:
        if value is not None:
            return assert_confident_presentation(value, "skill.summary_label")
        return value


class SkillGroupPreset(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    label: str = Field(min_length=2, max_length=80)
    items_text: str = Field(min_length=2, max_length=350)
    skill_ids: list[str] = Field(default_factory=list)
    factual_source: str = Field(min_length=3)

    @field_validator("items_text")
    @classmethod
    def validate_items_text(cls, value: str) -> str:
        return assert_confident_presentation(value, "skill_group.items_text")


class LanguageTranslation(StrictModel):
    language: str = Field(min_length=2, max_length=60)
    proficiency: str = Field(min_length=2, max_length=60)


class LanguageEntry(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    language: str = Field(min_length=2, max_length=60)
    proficiency: str = Field(min_length=2, max_length=60)
    translations: dict[SupportedLocale, LanguageTranslation] = Field(default_factory=dict)


class ContextualEquivalence(StrictModel):
    from_term: str = Field(min_length=2, max_length=80)
    to_term: str = Field(min_length=2, max_length=80)
    trigger_terms: list[str] = Field(min_length=1)
    factual_basis: str = Field(min_length=5, max_length=300)


class SectionLabels(StrictModel):
    summary: str = "RESUMO PROFISSIONAL"
    education: str = "FORMAÇÃO ACADÊMICA"
    experience: str = "EXPERIÊNCIA PROFISSIONAL"
    courses: str = "FORMAÇÃO COMPLEMENTAR"
    projects: str = "PROJETOS"
    skills: str = "COMPETÊNCIAS TÉCNICAS"
    languages: str = "IDIOMAS"


class AreaPreset(StrictModel):
    area_id: str = Field(pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(min_length=2, max_length=120)
    locale: SupportedLocale = "pt-BR"
    output_folder: str = Field(min_length=1, max_length=120)
    output_pdf_filename: str = Field(min_length=5, max_length=140, pattern=r"^[^/\\]+\.pdf$")
    show_age: bool = True
    show_neighborhood: bool = True
    max_pages: int = Field(default=1, ge=1, le=2)
    section_labels: SectionLabels = Field(default_factory=SectionLabels)
    section_order: list[SectionKind] = Field(
        default_factory=lambda: [
            "summary",
            "education",
            "experience",
            "courses",
            "skills",
            "languages",
        ]
    )
    page_break_before_sections: list[SectionKind] = Field(default_factory=list)
    default_summary: str = Field(min_length=20, max_length=900)
    summary_prefix: str = Field(min_length=10, max_length=400)
    allowed_summary_skill_ids: list[str] = Field(default_factory=list)
    preferred_bullet_ids: list[str] = Field(default_factory=list)
    max_bullets_per_experience: int = Field(default=4, ge=1, le=8)
    included_course_ids: list[str] = Field(default_factory=list)
    included_project_ids: list[str] = Field(default_factory=list)
    skill_groups: list[SkillGroupPreset] = Field(min_length=1)

    @field_validator("default_summary", "summary_prefix")
    @classmethod
    def validate_summary_texts(cls, value: str) -> str:
        return assert_confident_presentation(value, "area_preset.summary")

    @field_validator("section_order")
    @classmethod
    def validate_unique_sections(cls, value: list[SectionKind]) -> list[SectionKind]:
        if len(value) != len(set(value)):
            raise ValueError("section_order cannot contain duplicate section entries")
        return value


class MasterProfile(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    header: CandidateHeader
    education: list[EducationEntry] = Field(min_length=1)
    experiences: list[ExperienceEntry] = Field(default_factory=list)
    courses: list[CourseEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    skills: list[SkillItem] = Field(min_length=1)
    languages: list[LanguageEntry] = Field(min_length=1)
    contextual_equivalences: list[ContextualEquivalence] = Field(default_factory=list)
    forbidden_resume_terms: list[str] = Field(default_factory=list)
    area_presets: dict[str, AreaPreset] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_veracity_and_referential_integrity(self) -> MasterProfile:
        skills_by_id = {skill.id: skill for skill in self.skills}
        courses_by_id = {course.id: course for course in self.courses}
        projects_by_id = {project.id: project for project in self.projects}
        bullets_by_id = {
            bullet.id
            for exp in self.experiences
            for bullet in exp.bullets
        }

        if len(skills_by_id) != len(self.skills):
            raise ValueError("Duplicate skill id detected in skills catalog")
        if len(courses_by_id) != len(self.courses):
            raise ValueError("Duplicate course id detected in courses list")
        if len(projects_by_id) != len(self.projects):
            raise ValueError("Duplicate project id detected in projects list")

        reserved_project_names = [
            proj.name for proj in self.projects if proj.status == "reserved"
        ]
        all_forbidden = [*self.forbidden_resume_terms, *reserved_project_names]

        # Ensure no bullet or preset leaks forbidden or reserved project terms
        for exp in self.experiences:
            for bullet in exp.bullets:
                for forbidden in all_forbidden:
                    if forbidden and forbidden.casefold() in bullet.text.casefold():
                        raise ValueError(
                            f"Forbidden/reserved term '{forbidden}' found in bullet '{bullet.id}'"
                        )

        for key, preset in self.area_presets.items():
            if key != preset.area_id:
                raise ValueError(
                    f"Area preset key '{key}' does not match preset.area_id '{preset.area_id}'"
                )

            for course_id in preset.included_course_ids:
                course = courses_by_id.get(course_id)
                if not course:
                    raise ValueError(
                        f"Preset '{key}' references unknown course_id '{course_id}'"
                    )
                if course.status != "completed":
                    raise ValueError(
                        f"Preset '{key}' includes non-completed course '{course_id}' "
                        f"(status='{course.status}'). Only completed courses are allowed."
                    )

            for project_id in preset.included_project_ids:
                project = projects_by_id.get(project_id)
                if not project:
                    raise ValueError(
                        f"Preset '{key}' references unknown project_id '{project_id}'"
                    )
                if project.status != "approved":
                    raise ValueError(
                        f"Preset '{key}' includes unapproved/reserved project '{project_id}'"
                    )

            for skill_id in preset.allowed_summary_skill_ids:
                skill = skills_by_id.get(skill_id)
                if not skill:
                    raise ValueError(
                        f"Preset '{key}' references unknown summary skill_id '{skill_id}'"
                    )
                if not skill.can_appear_on_resume:
                    raise ValueError(
                        f"Preset '{key}' references non-resume skill '{skill_id}' "
                        f"(veracity='{skill.veracity}')"
                    )

            for group in preset.skill_groups:
                for skill_id in group.skill_ids:
                    skill = skills_by_id.get(skill_id)
                    if not skill:
                        raise ValueError(
                            f"Preset '{key}' group '{group.id}' references unknown skill_id '{skill_id}'"
                        )
                    if not skill.can_appear_on_resume:
                        raise ValueError(
                            f"Preset '{key}' group '{group.id}' includes non-resume skill "
                            f"'{skill_id}' (veracity='{skill.veracity}')"
                        )
                for forbidden in all_forbidden:
                    if forbidden and forbidden.casefold() in group.items_text.casefold():
                        raise ValueError(
                            f"Preset '{key}' group '{group.id}' contains forbidden term '{forbidden}'"
                        )

            for bullet_id in preset.preferred_bullet_ids:
                if bullet_id not in bullets_by_id:
                    raise ValueError(
                        f"Preset '{key}' references unknown preferred_bullet_id '{bullet_id}'"
                    )

            for forbidden in all_forbidden:
                if forbidden and forbidden.casefold() in preset.default_summary.casefold():
                    raise ValueError(
                        f"Preset '{key}' default_summary contains forbidden term '{forbidden}'"
                    )

        return self
