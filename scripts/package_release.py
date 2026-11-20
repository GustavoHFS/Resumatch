"""Package an allowlisted Windows release, never the local workspace or private data."""

from __future__ import annotations

import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT_FILES = {"Instalar.bat", "LICENSE", "README.md", "pyproject.toml", "DESIGN.md"}
PUBLIC_PREFIXES = ("backend/app/", "scripts/", "examples/", "assets/", "docs/")


def package_release(root: Path = ROOT, output: Path | None = None) -> Path:
    root = root.resolve()
    output = output or root / "build" / "Resumatch-Windows.zip"
    if not (root / "Resumatch.exe").is_file() or not (root / "frontend/dist/index.html").is_file():
        raise RuntimeError("Compile Resumatch.exe e frontend/dist antes de empacotar.")
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    files = {Path(name.decode("utf-8")) for name in tracked.stdout.split(b"\0") if name}
    files = {path for path in files if path.as_posix() in PUBLIC_ROOT_FILES
             or path.as_posix() == "backend/requirements.txt"
             or path.as_posix().startswith(PUBLIC_PREFIXES)}
    files.add(Path("Resumatch.exe"))
    files.update(path.relative_to(root) for path in (root / "frontend/dist").rglob("*") if path.is_file())
    forbidden_suffixes = {".sqlite3", ".db", ".pdf", ".docx", ".bak", ".pyc"}
    for path in files:
        if any(part in {"user_data", "node_modules", "__pycache__"} for part in path.parts) or (
            path.name.startswith(".env") or path.suffix.lower() in forbidden_suffixes
        ):
            raise RuntimeError("Arquivo privado ou inesperado bloqueado no pacote.")
        resolved = (root / path).resolve()
        if root not in resolved.parents or not resolved.is_file() or (root / path).is_symlink():
            raise RuntimeError("Caminho ausente ou inseguro no pacote.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(root / path, f"Resumatch/{path.as_posix()}")
    return output


if __name__ == "__main__":
    print(f"Pacote público criado: {package_release()}")
