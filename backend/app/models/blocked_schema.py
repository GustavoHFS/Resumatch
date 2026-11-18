"""Pydantic v2 schemas for the private employer blocklist."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from app.models.profile_schema import StrictModel

CNPJ_PATTERN = re.compile(r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}$")


class RelatedEntityRecord(StrictModel):
    name: str = Field(min_length=2, max_length=150)
    relationship_type: Literal[
        "corporate_group",
        "shared_ownership",
        "recruiting_on_behalf",
    ]
    evidence_source: str = Field(min_length=5, max_length=300)


class BlockedCompanyEntry(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(min_length=2, max_length=150)
    permanent: bool = True
    cnpj: str | None = None
    corporate_name: str | None = Field(default=None, max_length=180)
    domains: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    confirmed_related_entities: list[RelatedEntityRecord] = Field(default_factory=list)
    blocked_actions: list[str] = Field(
        default_factory=lambda: [
            "automated_application",
            "prospecting_email",
            "resume_submission",
            "interest_outreach",
        ]
    )
    require_evidence_for_relations: bool = True
    uncertain_relation_policy: Literal["hold_for_manual_review"] = "hold_for_manual_review"
    preserve_in_work_history: bool = True

    @field_validator("cnpj")
    @classmethod
    def validate_cnpj_format(cls, value: str | None) -> str | None:
        if value is not None and not CNPJ_PATTERN.match(value):
            raise ValueError("CNPJ must match XX.XXX.XXX/XXXX-XX format")
        return value


class BlockedCompaniesConfig(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    companies: list[BlockedCompanyEntry] = Field(default_factory=list)
