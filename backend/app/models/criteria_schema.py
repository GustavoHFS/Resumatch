"""Pydantic v2 schemas for deterministic job search & triage criteria."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.models.profile_schema import StrictModel


class AllowedCityRule(StrictModel):
    slug: str = Field(pattern=r"^[a-z0-9_-]+$")
    canonical_name: str = Field(min_length=2, max_length=80)
    state_code: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    aliases: list[str] = Field(min_length=1)
    neighborhoods: list[str] = Field(default_factory=list)


class GeographyPolicy(StrictModel):
    allowed_cities: list[AllowedCityRule] = Field(min_length=1)
    ambiguous_neighborhoods_without_city: list[str] = Field(default_factory=list)
    accept_remote_national: bool = True
    accept_remote_international_if_resident_allowed: bool = True
    reject_broader_metro_if_outside_allowed_cities: bool = True


class SchedulePolicy(StrictModel):
    earliest_hour: int = Field(default=6, ge=0, le=23)
    latest_hour: int = Field(default=18, ge=1, le=23)
    soft_latest_hour: int = Field(default=19, ge=1, le=23)
    reject_night_or_overnight: bool = True

    @model_validator(mode="after")
    def validate_hour_bounds(self) -> SchedulePolicy:
        if self.earliest_hour >= self.latest_hour:
            raise ValueError("earliest_hour must be strictly less than latest_hour")
        if self.soft_latest_hour < self.latest_hour:
            raise ValueError("soft_latest_hour must be >= latest_hour")
        return self


class ContractPolicy(StrictModel):
    accepted_types: list[str] = Field(
        default_factory=lambda: ["clt", "estagio_remunerado", "aprendiz"]
    )
    rejected_types: list[str] = Field(
        default_factory=lambda: [
            "pj",
            "mei",
            "freelancer",
            "voluntario",
            "nao_remunerado",
        ]
    )
    require_explicit_paid_internship_signal: bool = True
    allow_undisclosed_salary: bool = True


class CareerFocusPolicy(StrictModel):
    entry_level_signals: list[str] = Field(min_length=1)
    out_of_focus_seniority_signals: list[str] = Field(min_length=1)
    max_mandatory_experience_months: int = Field(default=24, ge=0, le=120)
    preferred_recency_days: int = Field(default=30, ge=1, le=365)
    equal_area_priority: bool = True
    max_secondary_sources_per_run: int = Field(default=1, ge=0, le=10)


class OmissionsPolicy(StrictModel):
    allow_local_prep_with_operational_omissions: bool = True
    never_assume_unconfirmed_remote_or_paid: bool = True


class ShortPrepRule(StrictModel):
    term: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(min_length=1)
    prerequisite_skill_ids: list[str] = Field(min_length=1)
    study_plan: str = Field(min_length=10, max_length=400)


class StrategicExclusionRule(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    urls: list[str] = Field(default_factory=list)
    excluded_source_terms: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=5, max_length=300)


class RoleCatalog(StrictModel):
    area_keywords: dict[str, list[str]] = Field(min_length=1)
    informal_hiring_signals: list[str] = Field(default_factory=list)
    grouped_search_queries: list[str] = Field(default_factory=list)


class SearchCriteria(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    include_remote: bool = True
    geography: GeographyPolicy
    schedule: SchedulePolicy = Field(default_factory=SchedulePolicy)
    contracts: ContractPolicy = Field(default_factory=ContractPolicy)
    career_focus: CareerFocusPolicy
    omissions_policy: OmissionsPolicy = Field(default_factory=OmissionsPolicy)
    role_catalog: RoleCatalog
    short_prep_rules: list[ShortPrepRule] = Field(default_factory=list)
    strategic_exclusions: list[StrategicExclusionRule] = Field(default_factory=list)
