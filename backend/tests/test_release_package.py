"""Release bundles exclude private files even when present in a source checkout."""

import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.package_release import package_release


def test_package_allowlist_never_copies_local_data(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    files = {
        "LICENSE": "MIT", "Instalar.bat": "@echo off",
        "backend/requirements.txt": "-e .", "backend/app/main.py": "# public",
        "frontend/dist/index.html": "<html>demo</html>", "Resumatch.exe": "demo executable",
        "user_data/profile/master_profile.json": '{"private":true}',
        ".env": "SECRET=demo", "notes.txt": "private note",
    }
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    output = package_release(root)
    with ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "Resumatch/Resumatch.exe" in names
        assert "Resumatch/frontend/dist/index.html" in names
        assert "Resumatch/backend/app/main.py" in names
        assert not any("user_data" in name or ".env" in name or "notes.txt" in name for name in names)
    assert (root / "user_data/profile/master_profile.json").read_text() == '{"private":true}'


def test_package_requires_compiled_assets(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Compile"):
        package_release(tmp_path)
