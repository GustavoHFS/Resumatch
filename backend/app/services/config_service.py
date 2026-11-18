"""Safe atomic JSON persistence with rotating timestamped backups in `user_data/backups/`."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from app.core.config import assert_safe_path


def atomic_write_validated_json(
    target_path: Path,
    validated_model: BaseModel,
    backups_dir: Path,
    max_backups: int = 10,
) -> str | None:
    """Atomically writes `validated_model` to `target_path` after snapshotting a rotating backup."""
    safe_target = assert_safe_path(target_path)
    safe_backups = assert_safe_path(backups_dir)
    safe_target.parent.mkdir(parents=True, exist_ok=True)
    safe_backups.mkdir(parents=True, exist_ok=True)

    backup_rel_name: str | None = None
    stem = safe_target.stem

    # 1. Snapshot existing file into rotating backup before writing
    if safe_target.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_file = safe_backups / f"{stem}.{stamp}.bak.json"
        shutil.copy2(safe_target, backup_file)
        backup_rel_name = backup_file.name

        # Prune older backups beyond max_backups
        existing_backups = sorted(safe_backups.glob(f"{stem}.*.bak.json"))
        if max_backups > 0 and len(existing_backups) > max_backups:
            for stale in existing_backups[: len(existing_backups) - max_backups]:
                try:
                    stale.unlink()
                except OSError:
                    pass

    # 2. Serialize + fsync to temporary file in same directory + atomic os.replace
    serialized = (
        json.dumps(validated_model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{stem}-",
        suffix=".tmp.json",
        dir=str(safe_target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, safe_target)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return backup_rel_name
