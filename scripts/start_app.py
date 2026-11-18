"""Single-process local launcher for Resumatch (FastAPI + bundled Vite SPA).

1. Ensures `frontend/dist/index.html` exists (runs `npm run build` automatically if missing).
2. Starts Uvicorn serving both `/api/*` and the static SPA on `http://127.0.0.1:8000`.
3. Waits for `/api/health` readiness and opens a dedicated desktop app window
   (`Edge/Chrome --app=http://127.0.0.1:8000`, falling back to `webbrowser.open`).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "frontend"
DIST_INDEX = FRONTEND_DIR / "dist" / "index.html"
BACKEND_DIR = REPO_ROOT / "backend"


def _find_npm_executable() -> str | None:
    venv_candidates = [
        REPO_ROOT / ".venv" / "Scripts" / "npm.cmd",
        REPO_ROOT / ".venv" / "bin" / "npm",
    ]
    for candidate in venv_candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("npm")


def ensure_frontend_build(force_rebuild: bool = False) -> None:
    if DIST_INDEX.is_file() and not force_rebuild:
        return

    npm_bin = _find_npm_executable()
    if not npm_bin:
        raise RuntimeError(
            "Frontend build (`frontend/dist/index.html`) not found and `npm` is unavailable."
        )

    env = os.environ.copy()
    venv_scripts = REPO_ROOT / ".venv" / "Scripts"
    venv_bin = REPO_ROOT / ".venv" / "bin"
    extra_paths = [str(p) for p in (venv_scripts, venv_bin) if p.is_dir()]
    if extra_paths:
        env["PATH"] = os.pathsep.join([*extra_paths, env.get("PATH", "")])

    if not (FRONTEND_DIR / "node_modules").is_dir():
        print("[Resumatch] Installing frontend dependencies...")
        subprocess.run([npm_bin, "install", "--no-audit", "--no-fund"], cwd=FRONTEND_DIR, env=env, check=True)

    print("[Resumatch] Building production SPA (`tsc -b && vite build`)...")
    subprocess.run([npm_bin, "run", "build"], cwd=FRONTEND_DIR, env=env, check=True)


def _find_app_mode_browser() -> str | None:
    if sys.platform == "win32":
        win_candidates = [
            shutil.which("msedge"),
            shutil.which("chrome"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        for path in win_candidates:
            if path and Path(path).is_file():
                return str(path)
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                return found
    return None


def _open_dedicated_window_when_ready(url: str) -> None:
    health_url = f"{url.rstrip('/')}/api/health"
    for _ in range(50):
        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.2)

    browser_exe = _find_app_mode_browser()
    if browser_exe:
        try:
            subprocess.Popen(
                [browser_exe, f"--app={url}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass

    webbrowser.open(url)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Resumatch local single-process app.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not launch browser window")
    parser.add_argument("--rebuild-ui", action="store_true", help="Force `npm run build` first")
    args = parser.parse_args()

    ensure_frontend_build(force_rebuild=args.rebuild_ui)

    sys.path.insert(0, str(BACKEND_DIR))
    import uvicorn

    app_url = f"http://{args.host}:{args.port}"
    print(f"[Resumatch] Starting local application at {app_url}")

    if not args.no_browser:
        threading.Thread(
            target=_open_dedicated_window_when_ready,
            args=(app_url,),
            daemon=True,
        ).start()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
