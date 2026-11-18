"""Installs and verifies the automatic `.git/hooks/pre-commit` privacy guard.

Generates a native Python `.git/hooks/pre-commit` hook (using direct interpreter
shebang on Windows to avoid MSYS2 overhead/restrictions, and `#!/usr/bin/env python3`
on POSIX) that runs `scripts/check_privacy.py --staged` on every `git commit`.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

HOOK_MARKER = "# RESUMATCH-PRIVACY-PRE-COMMIT-HOOK"


def _resolve_shebang(repo_root: Path) -> str:
    if os.name == "nt":
        venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
        interpreter = venv_python if venv_python.is_file() else Path(sys.executable)
        return f"#!{interpreter.resolve().as_posix()}"
    venv_posix = repo_root / ".venv" / "bin" / "python"
    if venv_posix.is_file():
        return f"#!{venv_posix.resolve().as_posix()}"
    return "#!/usr/bin/env python3"


def build_pre_commit_script(repo_root: Path) -> str:
    shebang = _resolve_shebang(repo_root)
    return f"""{shebang}
{HOOK_MARKER}
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    hook_file = Path(__file__).resolve()
    repo_root = hook_file.parents[2]
    scanner = repo_root / "scripts" / "check_privacy.py"
    if not scanner.is_file():
        print(f"[PRIVACY HOOK ERROR] Missing scanner at {{scanner}}", file=sys.stderr)
        return 1
    result = subprocess.run(
        [sys.executable, str(scanner), "--staged", "--repo-root", str(repo_root)],
        cwd=repo_root,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
"""


def install_pre_commit_hook(repo_root: Path, if_git_present: bool = False) -> bool:
    git_dir = repo_root / ".git"
    if not git_dir.is_dir():
        if if_git_present:
            print(
                f"[HOOK DEFERRED] No .git directory at {repo_root} yet. "
                "Run `python scripts/install_hooks.py` immediately after `git init`."
            )
            return False
        raise FileNotFoundError(
            f"No .git directory found at {repo_root}. Run `git init` first."
        )

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"

    script_text = build_pre_commit_script(repo_root)
    hook_path.write_bytes(script_text.replace("\r\n", "\n").encode("utf-8"))
    current_mode = os.stat(hook_path).st_mode
    os.chmod(hook_path, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"[HOOK INSTALLED] Active privacy pre-commit hook written to {hook_path}")
    return True


def is_hook_installed(repo_root: Path) -> bool:
    hook_path = repo_root / ".git" / "hooks" / "pre-commit"
    if not hook_path.is_file():
        return False
    return HOOK_MARKER in hook_path.read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root directory.",
    )
    parser.add_argument(
        "--if-git-present",
        action="store_true",
        help="Exit 0 with notice if .git has not been initialized yet.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check whether the privacy pre-commit hook is installed.",
    )
    parser.add_argument(
        "--init-git",
        action="store_true",
        help="Atomically run `git init` (if needed) and immediately install pre-commit hook.",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    if args.check:
        if is_hook_installed(repo_root):
            print("[HOOK OK] Privacy pre-commit hook is installed.")
            return 0
        print("[HOOK MISSING] Privacy pre-commit hook is not installed.", file=sys.stderr)
        return 1

    if args.init_git and not (repo_root / ".git").is_dir():
        import subprocess

        subprocess.run(["git", "init"], cwd=repo_root, check=True)
        print(f"[GIT INIT] Initialized Git repository at {repo_root}")

    try:
        install_pre_commit_hook(repo_root, if_git_present=args.if_git_present)
    except FileNotFoundError as exc:
        print(f"[HOOK ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
