"""pip install -e . must not turn generated public metadata into a PII leak."""

from pathlib import Path
import subprocess

import pytest

from scripts.check_privacy import PUBLIC_AUTHOR, check_path_policy, run_scan


@pytest.mark.parametrize("metadata_dir", ["resumatch.egg-info", "resumatch.dist-info"])
def test_clean_install_skips_untracked_metadata_not_public_sources(tmp_path: Path, metadata_dir: str) -> None:
    (tmp_path / "README.md").write_text(f"Developed by {PUBLIC_AUTHOR}\n", encoding="utf-8")
    generated = tmp_path / "backend" / metadata_dir / "PKG-INFO"
    generated.parent.mkdir(parents=True)
    generated.write_text(f"Author: {PUBLIC_AUTHOR}\n", encoding="utf-8")
    assert run_scan(tmp_path, tmp_path / "user_data") == ([], 1, 0)
    assert not (tmp_path / "user_data").exists()
    source = tmp_path / "backend" / "source.py"
    source.write_text(f"AUTHOR = '{PUBLIC_AUTHOR}'\n", encoding="utf-8")
    violations, _, _ = run_scan(tmp_path, tmp_path / "user_data")
    assert {violation.rel_path for violation in violations} == {"backend/source.py"}


@pytest.mark.parametrize("metadata_dir", ["resumatch.egg-info", "resumatch.dist-info"])
def test_tracked_and_staged_metadata_is_blocked(tmp_path: Path, metadata_dir: str) -> None:
    relative = Path("backend") / metadata_dir / "PKG-INFO"
    generated = tmp_path / relative
    generated.parent.mkdir(parents=True)
    generated.write_text(f"Author: {PUBLIC_AUTHOR}\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "--", relative.as_posix()], cwd=tmp_path, check=True, capture_output=True)
    assert check_path_policy(relative)[0].category == "FORBIDDEN_GENERATED_METADATA"
    for staged_only in [False, True]:
        violations, scanned, _ = run_scan(tmp_path, tmp_path / "user_data", staged_only=staged_only)
        assert scanned == 1
        assert [v.category for v in violations] == ["FORBIDDEN_GENERATED_METADATA"]
