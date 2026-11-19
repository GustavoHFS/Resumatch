"""Removes automatically captured administrative/logistics/commercial jobs from SQLite.

Manually imported jobs (`manual:*` and the legacy `descoberta_assistida` marker) are
always preserved so the user can keep adapting any pasted role in the Estúdio.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.domain.triage_engine import (  # noqa: E402
    contains_term,
    is_manual_source,
    is_non_tech_role,
)

DB_PATH = REPO_ROOT / "user_data" / "db" / "app.sqlite3"
CRITERIA_PATH = REPO_ROOT / "user_data" / "config" / "search_criteria.json"


def _tech_keywords() -> list[str]:
    data = json.loads(CRITERIA_PATH.read_text(encoding="utf-8"))
    keywords: list[str] = []
    for area, terms in (data.get("role_catalog", {}).get("area_keywords") or {}).items():
        if area in {"administrativo", "logistica", "comercial"}:
            continue
        keywords.extend(str(term) for term in terms)
    return keywords


def main() -> int:
    if not DB_PATH.is_file():
        print(f"Banco não encontrado: {DB_PATH}")
        return 1

    tech_terms = _tech_keywords()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "select id, title, area_id, primary_source from job_opportunities"
    ).fetchall()

    to_delete: list[str] = []
    for row in rows:
        if is_manual_source(row["primary_source"]):
            continue
        title = row["title"] or ""
        is_tech = any(contains_term(title, term) for term in tech_terms)
        if is_non_tech_role(title) or (row["area_id"] == "administrativo" and not is_tech):
            to_delete.append(row["id"])

    if to_delete:
        placeholders = ",".join("?" for _ in to_delete)
        conn.execute(
            f"delete from job_observations where opportunity_id in ({placeholders})",
            to_delete,
        )
        conn.execute(
            f"delete from application_events where opportunity_id in ({placeholders})",
            to_delete,
        )
        conn.execute(
            f"update resume_versions set opportunity_id = null "
            f"where opportunity_id in ({placeholders})",
            to_delete,
        )
        conn.execute(
            f"delete from application_tracking where opportunity_id in ({placeholders})",
            to_delete,
        )
        conn.execute(
            f"delete from job_opportunities where id in ({placeholders})",
            to_delete,
        )
        conn.commit()

    print(f"Vagas administrativas/logísticas removidas: {len(to_delete)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
