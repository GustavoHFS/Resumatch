"""Bootstraps `user_data/` from fictional `examples/*.example.json` files if empty.

Never overwrites existing private user files. Allows anyone cloning the GitHub
repository to start the application immediately in demonstration mode.
"""

from __future__ import annotations

import shutil
from pathlib import Path

EXAMPLE_MAPPINGS = [
    ("examples/master_profile.example.json", "profile/master_profile.json"),
    ("examples/search_criteria.example.json", "config/search_criteria.json"),
    ("examples/blocked_companies.example.json", "config/blocked_companies.json"),
    ("examples/sources.example.json", "config/sources.json"),
]


def bootstrap_demo_workspace(repo_root: Path, data_dir: Path | None = None) -> list[Path]:
    repo_root = repo_root.resolve()
    target_root = (data_dir or (repo_root / "user_data")).resolve()
    created: list[Path] = []

    for sub in ("profile", "config", "db", "exports"):
        (target_root / sub).mkdir(parents=True, exist_ok=True)

    for src_rel, dst_rel in EXAMPLE_MAPPINGS:
        src = repo_root / src_rel
        dst = target_root / dst_rel
        if not dst.exists() and src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            created.append(dst)

    return created


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    created = bootstrap_demo_workspace(repo_root)
    if created:
        print(f"[DEMO BOOTSTRAP] Created {len(created)} demo file(s) in user_data/.")
    else:
        print("[DEMO BOOTSTRAP] Existing user_data/ files preserved; nothing overwritten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
