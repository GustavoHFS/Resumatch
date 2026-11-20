"""Unified, Portuguese-BR Pydantic v2 schemas for the Base Resume (`MasterProfile` v2.1).

- Exclusively Portuguese-BR (single-language model).
- `courses` ("Cursos e Certificações"): `title`, `institution`, `workload` ("Carga Horária"),
  `completion_date` ("Conclusão"), and optional `description`.
- `projects`: `name`, optional `url`, and `description` (zero `role` / `period_or_role` field).
- `experiences`: `role`, `company`, `period`, `location`, and single rich free-text `description`.
- `skills`: simple deduplicated list of strings (`list[str]`).
"""

from __future__ import annotations

import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.domain.resume_policy import academic_order

SectionKind = Literal[
    "summary",
    "experience",
    "education",
    "projects",
    "courses",
    "skills",
    "languages",
]

ResumeFontFamily = Literal[
    "Libre Franklin",
    "Inter",
    "Arial",
    "Georgia",
    "Times New Roman",
]

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
HTTPS_URL_PATTERN = re.compile(r"^https://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _new_item_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SectionLabels(StrictModel):
    summary: str = "RESUMO PROFISSIONAL"
    experience: str = "EXPERIÊNCIA PROFISSIONAL"
    education: str = "FORMAÇÃO ACADÊMICA"
    projects: str = "PROJETOS"
    courses: str = "CURSOS E CERTIFICAÇÕES"
    skills: str = "COMPETÊNCIAS TÉCNICAS"
    languages: str = "IDIOMAS"


class ProfileLink(StrictModel):
    label: str = Field(default="Link", min_length=1, max_length=60)
    url: str = Field(min_length=10, max_length=300)
    display_text: str = Field(default="", max_length=160)

    @field_validator("url")
    @classmethod
    def validate_https_url(cls, value: str) -> str:
        if not HTTPS_URL_PATTERN.match(value):
            raise ValueError("URL do link deve usar https://")
        return value

    @model_validator(mode="after")
    def ensure_display_text(self) -> ProfileLink:
        if not self.display_text:
            cleaned = re.sub(r"^https://(?:www\.)?", "", self.url, flags=re.IGNORECASE).rstrip("/")
            self.display_text = cleaned[:160] or self.url
        return self


class CandidateHeader(StrictModel):
    full_name: str = Field(default="", max_length=140)
    email: str = Field(default="", max_length=150)
    phone: str = Field(default="", max_length=50)
    city: str = Field(default="", max_length=90)
    state: str = Field(default="", max_length=90)
    neighborhood: str | None = Field(default=None, max_length=90)
    country: str = Field(default="Brasil", max_length=80)
    age: int | None = Field(default=None, ge=14, le=100)
    links: list[ProfileLink] = Field(default_factory=list, max_length=10)

    @field_validator("email")
    @classmethod
    def validate_optional_email(cls, value: str) -> str:
        if value and not EMAIL_PATTERN.match(value):
            raise ValueError("Formato de e-mail inválido")
        return value


class ExperienceEntry(StrictModel):
    id: str = Field(default_factory=lambda: _new_item_id("exp"), min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=160)
    company: str = Field(default="", max_length=160)
    period: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=8000)


class EducationEntry(StrictModel):
    id: str = Field(default_factory=lambda: _new_item_id("edu"), min_length=1, max_length=80)
    degree: str = Field(min_length=1, max_length=180)
    institution: str = Field(default="", max_length=180)
    period: str = Field(default="", max_length=140)
    details: str = Field(default="", max_length=600)


class CourseEntry(StrictModel):
    id: str = Field(default_factory=lambda: _new_item_id("crs"), min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=180)
    institution: str = Field(default="", max_length=160)
    workload: str = Field(default="", max_length=80)
    completion_date: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=1500)


class ProjectEntry(StrictModel):
    id: str = Field(default_factory=lambda: _new_item_id("prj"), min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    url: str | None = Field(default=None, max_length=300)
    description: str = Field(default="", max_length=3000)

    @field_validator("url")
    @classmethod
    def validate_optional_project_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not HTTPS_URL_PATTERN.match(value):
            raise ValueError("URL do projeto deve usar https://")
        return value


class LanguageEntry(StrictModel):
    id: str = Field(default_factory=lambda: _new_item_id("lng"), min_length=1, max_length=80)
    language: str = Field(min_length=1, max_length=80)
    proficiency: str = Field(default="", max_length=80)


class MasterProfile(StrictModel):
    schema_version: Literal["2.1"] = "2.1"
    header: CandidateHeader
    summary: str = Field(default="", max_length=4000)
    experiences: list[ExperienceEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    education_order_manual: bool = False
    courses: list[CourseEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    languages: list[LanguageEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def default_academic_priority(self) -> MasterProfile:
        if not self.education_order_manual:
            self.education = academic_order(self.education)
        return self

    @field_validator("skills")
    @classmethod
    def normalize_and_deduplicate_skills(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            tag = raw.strip()
            if not tag:
                continue
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                cleaned.append(tag[:120])
        return cleaned
