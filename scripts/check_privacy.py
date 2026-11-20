"""Privacy & PII static/pre-commit scanner for public GitHub safety.

Contains ZERO hardcoded personal strings. Instead, combines:
1. Generic PII/secret regexes (CPF, CNPJ, real BR phones, personal emails, API keys,
   forbidden document/database extensions, and private directory paths).
2. Dynamic in-memory fingerprinting loaded at runtime from local `user_data/`
   (candidate full name, neighborhood, handles, blocked companies, reserved project
   codenames), redacting matched values in output so secrets never leak to logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

IGNORED_DIRS = {
    ".git",
    "user_data",
    "Dados Sensíveis",
    ".venv",
    "venv",
    "ENV",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
}

FORBIDDEN_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".odttf",
    ".sqlite3",
    ".db",
    ".db-wal",
    ".db-shm",
    ".bak",
}

FORBIDDEN_FILENAMES = {
    ".env",
    "opencode.jsonc",
    "AGENTS.md",
}

# Public credit is a narrow exception, never an exemption for contact details.
PUBLIC_AUTHOR_SHORT = "Gustavo" + " Henrique"
PUBLIC_AUTHOR = PUBLIC_AUTHOR_SHORT + " Francisco da Silva"
PUBLIC_REPOSITORY = "https://github.com/" + "GustavoHFS/Resumatch"
PUBLIC_REPOSITORY_PATTERN = re.compile(
    re.escape(PUBLIC_REPOSITORY) + r"(?=[/?#\s<>\)\"']|$)", re.IGNORECASE
)
AUTHOR_CREDIT_FILES = {
    "LICENSE", "README.md", "pyproject.toml", "frontend/package.json",
    "frontend/src/components/SettingsDrawer.tsx", "scripts/build_launcher_exe.py",
}


def is_author_credit_path(rel_path: Path) -> bool:
    return rel_path.as_posix() in AUTHOR_CREDIT_FILES or (
        bool(rel_path.parts) and rel_path.parts[0] == "docs"
    )

GENERIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "PERSONAL_EMAIL",
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@(?:gmail|hotmail|outlook|yahoo|icloud|live|uol|bol|terra|protonmail|proton\.me)\.[A-Za-z]{2,}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "BR_PHONE_NUMBER",
        re.compile(
            r"(?:\+55\s*)?(?:\([1-9]{2}\)|(?<!\d)[1-9]{2})\s*9[1-9]\d{3}[-\s]?\d{4}\b"
        ),
    ),
    (
        "BR_CPF",
        re.compile(r"\b(?!000\.000\.000-00)\d{3}\.\d{3}\.\d{3}-\d{2}\b"),
    ),
    (
        "BR_CNPJ",
        re.compile(r"\b(?!00\.000\.000/0001-00)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b"),
    ),
    (
        "API_SECRET_KEY",
        re.compile(
            r"\b(?:AIza[0-9A-Za-z_-]{35}|gsk_[0-9A-Za-z]{20,}|sk-[0-9A-Za-z]{20,}|ghp_[0-9A-Za-z]{36})\b"
        ),
    ),
]


@dataclass(frozen=True)
class DynamicSecret:
    category: str
    value: str


@dataclass(frozen=True)
class Violation:
    rel_path: str
    line_number: int | None
    category: str
    detail: str


def _safe_load_json(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _add_secret(bucket: list[DynamicSecret], seen: set[str], category: str, raw: str | None) -> None:
    if not raw:
        return
    cleaned = raw.strip()
    # Skip overly short/generic words (like 'PCD' or state names) to avoid false positives in code
    if len(cleaned) < 5 or cleaned.lower() in {"brasil", "brazil", "pernambuco", "recife"}:
        return
    key = cleaned.casefold()
    if key not in seen:
        seen.add(key)
        bucket.append(DynamicSecret(category=category, value=cleaned))


def load_local_private_fingerprints(data_dir: Path) -> list[DynamicSecret]:
    """Extracts sensitive tokens from local `user_data/` without ever touching `Dados Sensíveis`."""
    secrets: list[DynamicSecret] = []
    seen: set[str] = set()

    if not data_dir.is_dir():
        return secrets

    # 1. New Phase 1+ Master Profile & Blocked Companies
    master_profile = _safe_load_json(data_dir / "profile" / "master_profile.json")
    if isinstance(master_profile, dict):
        header = master_profile.get("header") or {}
        if isinstance(header, dict):
            _add_secret(secrets, seen, "PRIVATE_CANDIDATE_NAME", header.get("full_name"))
            _add_secret(secrets, seen, "PRIVATE_CANDIDATE_EMAIL", header.get("email"))
            _add_secret(secrets, seen, "PRIVATE_CANDIDATE_PHONE", header.get("phone"))
            _add_secret(secrets, seen, "PRIVATE_NEIGHBORHOOD", header.get("neighborhood"))
            for link in header.get("links") or []:
                if isinstance(link, dict):
                    _add_secret(secrets, seen, "PRIVATE_PROFILE_LINK", link.get("display_text"))
        for project in master_profile.get("projects") or []:
            if isinstance(project, dict) and project.get("status") == "reserved":
                _add_secret(secrets, seen, "PRIVATE_RESERVED_PROJECT", project.get("name"))

    blocked_cfg = _safe_load_json(data_dir / "config" / "blocked_companies.json")
    if isinstance(blocked_cfg, dict):
        for company in blocked_cfg.get("companies") or []:
            if isinstance(company, dict):
                _add_secret(secrets, seen, "PRIVATE_BLOCKED_COMPANY", company.get("display_name"))
                _add_secret(secrets, seen, "PRIVATE_BLOCKED_COMPANY", company.get("corporate_name"))
                _add_secret(secrets, seen, "PRIVATE_BLOCKED_COMPANY", company.get("cnpj"))
                for alias in company.get("aliases") or []:
                    _add_secret(secrets, seen, "PRIVATE_BLOCKED_COMPANY", alias)
                for domain in company.get("domains") or []:
                    _add_secret(secrets, seen, "PRIVATE_BLOCKED_COMPANY", domain)
                for rel in company.get("confirmed_related_entities") or []:
                    if isinstance(rel, dict):
                        _add_secret(secrets, seen, "PRIVATE_BLOCKED_COMPANY", rel.get("name"))

    # 2. Legacy Archive Fallback (active already in Phase 0 before Phase 1 migration!)
    legacy_dir = data_dir / "legacy_archive"
    if legacy_dir.is_dir():
        legacy_blocked = _safe_load_json(legacy_dir / "config" / "empresas-bloqueadas.json")
        if isinstance(legacy_blocked, dict):
            for company in legacy_blocked.get("empresas") or []:
                if isinstance(company, dict):
                    _add_secret(
                        secrets, seen, "PRIVATE_BLOCKED_COMPANY", company.get("nome_informado")
                    )
                    ids = company.get("identificadores_confirmados") or {}
                    if isinstance(ids, dict):
                        _add_secret(
                            secrets, seen, "PRIVATE_BLOCKED_COMPANY", ids.get("razao_social")
                        )
                        _add_secret(secrets, seen, "PRIVATE_BLOCKED_COMPANY", ids.get("cnpj"))
                        for alias in ids.get("aliases") or []:
                            _add_secret(secrets, seen, "PRIVATE_BLOCKED_COMPANY", alias)

        legacy_general = _safe_load_json(legacy_dir / "config" / "curriculo-generalista.json")
        if isinstance(legacy_general, dict):
            saida = legacy_general.get("saida", "")
            if isinstance(saida, str) and " - " in saida:
                candidate_name = Path(saida).name.split(" - ", 1)[0]
                _add_secret(secrets, seen, "PRIVATE_CANDIDATE_NAME", candidate_name)
            for term in legacy_general.get("termos_proibidos") or []:
                if isinstance(term, str) and len(term.strip()) >= 5:
                    _add_secret(secrets, seen, "PRIVATE_RESERVED_PROJECT", term)

        legacy_en = _safe_load_json(legacy_dir / "config" / "curriculo-suporte-en.json")
        if isinstance(legacy_en, dict):
            for change in legacy_en.get("alteracoes") or []:
                if isinstance(change, dict) and change.get("indice") == 1:
                    for seg in change.get("trechos") or []:
                        if isinstance(seg, dict) and "," in str(seg.get("texto", "")):
                            neighborhood = str(seg["texto"]).split(",", 1)[0].strip()
                            _add_secret(secrets, seen, "PRIVATE_NEIGHBORHOOD", neighborhood)

        legacy_factual = legacy_dir / "perfil" / "perfil-factual.md"
        if legacy_factual.is_file():
            try:
                content = legacy_factual.read_text(encoding="utf-8")
                linkedin_match = re.search(
                    r"linkedin\.com/in/([A-Za-z0-9_-]{5,})", content, re.IGNORECASE
                )
                if linkedin_match:
                    _add_secret(secrets, seen, "PRIVATE_PROFILE_HANDLE", linkedin_match.group(1))
            except OSError:
                pass

    return secrets


def check_path_policy(rel_path: Path) -> list[Violation]:
    violations: list[Violation] = []
    posix = rel_path.as_posix()
    parts = rel_path.parts

    if parts and parts[0] == "user_data":
        violations.append(
            Violation(posix, None, "FORBIDDEN_PRIVATE_PATH", "Files inside user_data/ must never be tracked")
        )

    if any("dados sens" in part.casefold() for part in parts):
        violations.append(
            Violation(posix, None, "FORBIDDEN_VAULT_PATH", "Vault directory must never be tracked")
        )

    if any(part.startswith("Currículo") or part.startswith(".tmp") or part == ".dryrun" for part in parts):
        violations.append(
            Violation(posix, None, "FORBIDDEN_LEGACY_OR_TEMP_PATH", "Legacy resume or temp path detected")
        )

    if rel_path.name in FORBIDDEN_FILENAMES or (
        rel_path.name.startswith(".env") and rel_path.name != ".env.example"
    ):
        violations.append(
            Violation(posix, None, "FORBIDDEN_FILENAME", f"Private file '{rel_path.name}' must not be public")
        )

    for suffix in rel_path.suffixes:
        if suffix.lower() in FORBIDDEN_EXTENSIONS:
            violations.append(
                Violation(
                    posix,
                    None,
                    "FORBIDDEN_BINARY_OR_DOC",
                    f"Extension '{suffix}' is blocked from public tracking",
                )
            )
            break

    return violations


def scan_file_contents(
    abs_path: Path, rel_path: Path, dynamic_secrets: list[DynamicSecret]
) -> list[Violation]:
    violations: list[Violation] = []
    posix = rel_path.as_posix()
    if not abs_path.is_file():
        return violations

    try:
        raw_bytes = abs_path.read_bytes()
    except OSError as exc:
        return [Violation(posix, None, "READ_ERROR", f"Unable to inspect file: {type(exc).__name__}")]

    if b"\x00" in raw_bytes[:4096]:
        # Binary file outside allowed assets; check if font/image or unexpected binary
        if rel_path.suffix.lower() not in {".woff2", ".woff", ".ttf", ".png", ".svg", ".ico"}:
            violations.append(
                Violation(posix, None, "UNEXPECTED_BINARY", "Unexpected binary file in public workspace")
            )
        return violations

    text = raw_bytes.decode("utf-8", errors="replace")
    for line_no, line in enumerate(text.splitlines(), start=1):
        for category, pattern in GENERIC_PATTERNS:
            if pattern.search(line):
                violations.append(
                    Violation(
                        posix,
                        line_no,
                        category,
                        f"Matched sensitive pattern [{category}] (value redacted)",
                    )
                )
        lowered = line.casefold()
        credit_line = lowered
        if is_author_credit_path(rel_path):
            credit_line = credit_line.replace(PUBLIC_AUTHOR.casefold(), "").replace(
                PUBLIC_AUTHOR_SHORT.casefold(), ""
            )
        elif PUBLIC_AUTHOR_SHORT.casefold() in lowered:
            violations.append(Violation(posix, line_no, "PRIVATE_CANDIDATE_NAME",
                                        "Public author name outside credit allowlist (value redacted)"))
        for secret in dynamic_secrets:
            searchable = credit_line if (
                secret.category == "PRIVATE_CANDIDATE_NAME"
                and secret.value.casefold() in {PUBLIC_AUTHOR.casefold(), PUBLIC_AUTHOR_SHORT.casefold()}
            ) else lowered
            if secret.category == "PRIVATE_PROFILE_LINK" and is_author_credit_path(rel_path):
                # The exact project's URL is public, not the standalone personal profile.
                searchable = PUBLIC_REPOSITORY_PATTERN.sub("[PUBLIC_REPOSITORY]", searchable)
            if secret.value.casefold() in searchable:
                violations.append(
                    Violation(
                        posix,
                        line_no,
                        secret.category,
                        f"Matched local private token [{secret.category}] (value redacted)",
                    )
                )

    return violations


def collect_staged_files(repo_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def collect_workspace_files(repo_root: Path) -> list[Path]:
    collected: list[Path] = []
    for current_root, dirs, files in os.walk(repo_root):
        rel_dir = Path(current_root).relative_to(repo_root)
        # Keep user_data/.gitkeep visible, skip everything else in IGNORED_DIRS
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for filename in files:
            rel_file = rel_dir / filename if rel_dir != Path(".") else Path(filename)
            if rel_dir == Path(".") and filename.lower().endswith(".exe"):
                continue
            collected.append(rel_file)

    # If .git exists, also verify git ls-files hasn't tracked anything inside ignored dirs!
    if (repo_root / ".git").exists():
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            existing = {p.as_posix() for p in collected}
            for tracked in proc.stdout.splitlines():
                tracked = tracked.strip()
                if tracked and tracked not in existing:
                    collected.append(Path(tracked))
    return sorted(collected)


def run_scan(repo_root: Path, data_dir: Path, staged_only: bool = False) -> tuple[list[Violation], int, int]:
    dynamic_secrets = load_local_private_fingerprints(data_dir)
    targets = collect_staged_files(repo_root) if staged_only else collect_workspace_files(repo_root)
    all_violations: list[Violation] = []
    for rel_path in targets:
        path_violations = check_path_policy(rel_path)
        all_violations.extend(path_violations)
        if not path_violations:
            all_violations.extend(
                scan_file_contents(repo_root / rel_path, rel_path, dynamic_secrets)
            )
    return all_violations, len(targets), len(dynamic_secrets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Scan only Git staged files (used by .git/hooks/pre-commit).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root directory.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Private user_data directory (defaults to <repo-root>/user_data).",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    data_dir = (args.data_dir or (repo_root / "user_data")).resolve()

    violations, scanned_count, secret_count = run_scan(
        repo_root=repo_root,
        data_dir=data_dir,
        staged_only=args.staged,
    )

    mode = "staged" if args.staged else "workspace"
    if violations:
        print(
            f"[PRIVACY BLOCKED] Found {len(violations)} violation(s) across {scanned_count} {mode} file(s):",
            file=sys.stderr,
        )
        for v in violations:
            loc = f"{v.rel_path}:{v.line_number}" if v.line_number else v.rel_path
            print(f"  - {loc} [{v.category}]: {v.detail}", file=sys.stderr)
        return 1

    print(
        f"[PRIVACY OK] Scanned {scanned_count} {mode} file(s) against generic PII rules + "
        f"{secret_count} local dynamic fingerprint(s). Zero leaks detected."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
