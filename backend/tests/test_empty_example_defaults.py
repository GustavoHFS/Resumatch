"""Os exemplos enviados ao usuário não incluem identidade ou bloqueios pré-configurados."""

from __future__ import annotations

import json
from pathlib import Path

from app.models import BlockedCompaniesConfig, MasterProfile, SearchCriteria

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def test_new_user_profile_is_blank_and_schema_compatible() -> None:
    raw = json.loads((EXAMPLES / "master_profile.example.json").read_text(encoding="utf-8"))
    profile = MasterProfile.model_validate(raw)
    assert all(not getattr(profile.header, field) for field in ("full_name", "email", "phone", "city", "state"))
    assert profile.header.links == []
    assert profile.summary == ""
    assert profile.experiences == profile.education == profile.courses == []
    assert profile.projects == profile.skills == profile.languages == []


def test_new_user_filters_are_neutral_and_blocklist_is_empty() -> None:
    raw = json.loads((EXAMPLES / "search_criteria.example.json").read_text(encoding="utf-8"))
    criteria = SearchCriteria.model_validate(raw)
    assert (criteria.target_state, criteria.target_cities, criteria.keywords) == ("", [], [])
    assert criteria.geography.allowed_cities == []
    assert criteria.include_remote is True
    assert set(criteria.role_catalog.area_keywords).isdisjoint({"administrativo", "logistica"})
    blocked_raw = json.loads((EXAMPLES / "blocked_companies.example.json").read_text(encoding="utf-8"))
    assert blocked_raw == {"blocked_companies": []}
    assert BlockedCompaniesConfig.model_validate(blocked_raw).companies == []
