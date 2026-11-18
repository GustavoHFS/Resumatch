"""Migrates `user_data/config/sources.json` to the v2.1 4-category catalog (with backup)
and deduplicates cross-portal duplicates + normalizes `published_at` timestamps in `user_data/db/app.sqlite3`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlmodel import Session, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.database import create_sqlite_wal_engine  # noqa: E402
from app.domain.date_utils import normalize_published_iso  # noqa: E402
from app.domain.triage_engine import compute_cross_portal_signature  # noqa: E402
from app.models import (  # noqa: E402
    ApplicationEvent,
    ApplicationTracking,
    JobObservation,
    JobOpportunity,
    JobSourcesConfig,
    ResumeVersion,
)


def migrate_sources_config() -> dict[str, Any]:
    settings = get_settings()
    sources_path = settings.sources_path
    example_path = REPO_ROOT / "examples" / "sources.example.json"
    template_data = json.loads(example_path.read_text(encoding="utf-8"))
    validated = JobSourcesConfig.model_validate(template_data)

    backup_name: str | None = None
    if sources_path.is_file():
        backups_dir = settings.backups_dir
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_name = f"sources.pre_v21_{stamp}.bak.json"
        shutil.copy2(sources_path, backups_dir / backup_name)

    sources_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(validated.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    fd, tmp_str = tempfile.mkstemp(prefix=".sources_v21_", suffix=".tmp", dir=str(sources_path.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, sources_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    return {
        "sources_count": len(validated.sources),
        "categories": sorted({s.category for s in validated.sources}),
        "backup_snapshot": backup_name,
    }


def cleanup_sqlite_jobs() -> dict[str, Any]:
    settings = get_settings()
    db_path = settings.db_path
    if not db_path.is_file():
        return {"status": "skipped", "reason": "app.sqlite3 not found"}

    backups_dir = settings.backups_dir
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_backup_name = f"app.pre_v21_{stamp}.bak.sqlite3"
    shutil.copy2(db_path, backups_dir / db_backup_name)

    engine = create_sqlite_wal_engine(db_path)
    normalized_dates = 0
    merged_duplicates = 0

    try:
        with Session(engine) as session:
            all_opps = session.exec(select(JobOpportunity)).all()
            # 1. Normalize all `published_at` timestamps to canonical UTC ISO
            for opp in all_opps:
                norm_pub = normalize_published_iso(opp.published_at, fallback_iso=opp.discovered_at)
                if norm_pub and norm_pub != opp.published_at:
                    opp.published_at = norm_pub
                    normalized_dates += 1
                    session.add(opp)
            session.flush()

            # 2. Deduplicate cross-portal duplicates keeping tracked / richest / newest record
            by_sig: dict[str, list[JobOpportunity]] = {}
            for opp in all_opps:
                sig = compute_cross_portal_signature(
                    opp.company,
                    opp.title,
                    opp.location,
                    opp.workplace_mode,
                )
                if sig:
                    by_sig.setdefault(sig, []).append(opp)

            tracked_ids = {
                row.opportunity_id for row in session.exec(select(ApplicationTracking)).all()
            }

            for sig, group in by_sig.items():
                if len(group) < 2:
                    continue
                # Sort best survivor first: tracked > non-default stage > longest description > newest published_at
                group.sort(
                    key=lambda o: (
                        o.id in tracked_ids,
                        o.application_stage not in {"descoberta", ""},
                        len(o.description_text or ""),
                        o.published_at or "",
                    ),
                    reverse=True,
                )
                survivor = group[0]
                if not survivor.content_fingerprint:
                    survivor.content_fingerprint = sig
                    session.add(survivor)

                for dup in group[1:]:
                    # Re-point observations, events, and resume versions to survivor
                    for obs in session.exec(
                        select(JobObservation).where(JobObservation.opportunity_id == dup.id)
                    ).all():
                        obs.opportunity_id = survivor.id
                        session.add(obs)
                    for evt in session.exec(
                        select(ApplicationEvent).where(ApplicationEvent.opportunity_id == dup.id)
                    ).all():
                        evt.opportunity_id = survivor.id
                        session.add(evt)
                    for ver in session.exec(
                        select(ResumeVersion).where(ResumeVersion.opportunity_id == dup.id)
                    ).all():
                        ver.opportunity_id = survivor.id
                        session.add(ver)
                    dup_trk = session.get(ApplicationTracking, dup.id)
                    if dup_trk is not None:
                        session.delete(dup_trk)
                    session.delete(dup)
                    merged_duplicates += 1

            session.commit()
            remaining = len(session.exec(select(JobOpportunity)).all())
    finally:
        engine.dispose()

    return {
        "status": "cleaned",
        "db_backup": db_backup_name,
        "normalized_published_dates": normalized_dates,
        "merged_cross_portal_duplicates": merged_duplicates,
        "remaining_opportunities": remaining,
    }


def main() -> int:
    report = {
        "sources": migrate_sources_config(),
        "database": cleanup_sqlite_jobs(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
