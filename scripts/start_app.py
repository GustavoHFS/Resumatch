"""Windowless background single-process launcher for Resumatch v2.2.

- Runs silently in the background via `pythonw.exe` (redirecting `None` stdio streams safely
  so Uvicorn never fails on `isatty()`, and passing `CREATE_NO_WINDOW` to child commands).
- Validates `"project": "Resumatch"` and `"version": "2.2.0"` on `/api/health`:
  * Reuses an already-running compatible Resumatch server.
  * Evicts stale older Resumatch instances on port 8000.
  * Automatically falls back to the next free port in `8001..8010` if port 8000 is used by another app.
- Launches dedicated `--app` window with Edge Startup Boost flags (`--disable-background-mode`,
  `--disable-extensions`, `--no-service-autorun`).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import socket
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
EXPECTED_API_VERSION = "2.2.0"
SW_SHOWMINNOACTIVE = 7
WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _ensure_safe_stdio_and_minimized_console() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

    if sys.platform == "win32":
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
        except Exception:
            pass


def _is_port_bound(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex((host, port)) == 0


def _probe_port_owner(host: str, port: int) -> str:
    """Returns `'compatible_resumatch'`, `'stale_resumatch'`, `'foreign_app'`, or `'free'`."""
    if not _is_port_bound(host, port):
        return "free"
    health_url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(health_url, timeout=0.9) as resp:
            if resp.status != 200:
                return "foreign_app"
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(body, dict) and body.get("project") == "Resumatch":
                if body.get("version") == EXPECTED_API_VERSION and "ai_connected" in body:
                    return "compatible_resumatch"
                return "stale_resumatch"
            return "foreign_app"
    except Exception:
        return "foreign_app"


def _terminate_stale_listener_on_port(port: int) -> None:
    my_pid = os.getpid()
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                creationflags=WIN_NO_WINDOW,
                check=False,
            )
            pids: set[int] = set()
            pattern = re.compile(rf":({port})\s+\S+\s+LISTENING\s+(\d+)", re.IGNORECASE)
            for line in proc.stdout.splitlines():
                m = pattern.search(line)
                if m:
                    pid = int(m.group(2))
                    if pid > 4 and pid != my_pid:
                        pids.add(pid)
            for pid in pids:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=WIN_NO_WINDOW,
                    check=False,
                )
        except Exception:
            pass
    else:
        try:
            proc = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True,
                text=True,
                check=False,
            )
            for raw_pid in proc.stdout.splitlines():
                if raw_pid.strip().isdigit() and int(raw_pid.strip()) != my_pid:
                    subprocess.run(["kill", "-9", raw_pid.strip()], check=False)
        except Exception:
            pass

    for _ in range(15):
        if not _is_port_bound("127.0.0.1", port):
            break
        time.sleep(0.15)


def _select_target_port(host: str, preferred_port: int) -> tuple[int, bool]:
    """Returns `(selected_port, already_running_compatible)` across `preferred_port..8010`."""
    owner = _probe_port_owner(host, preferred_port)
    if owner == "compatible_resumatch":
        return preferred_port, True
    if owner == "stale_resumatch":
        _terminate_stale_listener_on_port(preferred_port)
        if not _is_port_bound(host, preferred_port):
            return preferred_port, False
    if owner == "free":
        return preferred_port, False

    for candidate in range(max(8001, preferred_port + 1), 8011):
        cand_owner = _probe_port_owner(host, candidate)
        if cand_owner == "compatible_resumatch":
            return candidate, True
        if cand_owner == "free":
            return candidate, False
    return preferred_port, False


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
        subprocess.run(
            [npm_bin, "install", "--no-audit", "--no-fund"],
            cwd=FRONTEND_DIR,
            env=env,
            creationflags=WIN_NO_WINDOW,
            check=True,
        )

    subprocess.run(
        [npm_bin, "run", "build"],
        cwd=FRONTEND_DIR,
        env=env,
        creationflags=WIN_NO_WINDOW,
        check=True,
    )


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


def _open_app_window(url: str) -> None:
    browser_exe = _find_app_mode_browser()
    if browser_exe:
        profile_dir = REPO_ROOT / "user_data" / "browser_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(
                [
                    browser_exe,
                    f"--app={url}",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-mode",
                    "--disable-extensions",
                    "--no-service-autorun",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=WIN_NO_WINDOW,
            )
            return
        except OSError:
            pass
    webbrowser.open(url)


def _open_dedicated_window_when_ready(host: str, port: int) -> None:
    url = f"http://{host}:{port}"
    for _ in range(60):
        if _probe_port_owner(host, port) == "compatible_resumatch":
            break
        time.sleep(0.2)
    _open_app_window(url)


def main() -> int:
    _ensure_safe_stdio_and_minimized_console()

    parser = argparse.ArgumentParser(description="Start Resumatch local single-process app.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Preferred bind port (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not launch browser window")
    parser.add_argument("--rebuild-ui", action="store_true", help="Force `npm run build` first")
    args = parser.parse_args()

    selected_port, already_running = _select_target_port(args.host, args.port)
    app_url = f"http://{args.host}:{selected_port}"

    if not args.rebuild_ui and already_running:
        if not args.no_browser:
            _open_app_window(app_url)
        return 0

    ensure_frontend_build(force_rebuild=args.rebuild_ui)

    sys.path.insert(0, str(BACKEND_DIR))
    import uvicorn

    if not args.no_browser:
        threading.Thread(
            target=_open_dedicated_window_when_ready,
            args=(args.host, selected_port),
            daemon=True,
        ).start()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=selected_port,
        access_log=False,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
