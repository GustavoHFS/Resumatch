"""Application settings and path isolation guards powered by Pydantic Settings."""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


def _normalize_token(value: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFD", value.casefold())
        if not unicodedata.combining(c)
    )


def assert_safe_path(path: Path) -> Path:
    """Blocks access to any local credential vault directory ('Dados Sensíveis' / 'vault')."""
    for part in path.parts:
        norm_part = _normalize_token(part)
        if norm_part in {"dados sensiveis", "vault"}:
            raise PermissionError("Access to local sensitive vault is strictly prohibited.")
    resolved = path.resolve()
    for part in resolved.parts:
        norm_part = _normalize_token(part)
        if norm_part in {"dados sensiveis", "vault"}:
            raise PermissionError("Access to local sensitive vault is strictly prohibited.")
    return resolved


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_name: str = "Resumatch"
    version: str = "2.2.0"
    data_dir: Path = Field(default=REPO_ROOT / "user_data")
    examples_dir: Path = Field(default=REPO_ROOT / "examples")
    frontend_dist_dir: Path = Field(default=REPO_ROOT / "frontend" / "dist")

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    frontend_origin: str = "http://localhost:5173"

    scheduler_enabled: bool = False
    scheduler_default_interval_minutes: int = 360
    pdf_browser_idle_timeout_seconds: int = 120

    llm_timeout_seconds: float = 25.0
    max_config_backups: int = 10
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_fallback_model: str = "gemini-2.5-flash-lite"

    @property
    def ai_connected(self) -> bool:
        return bool(self.gemini_api_key.strip())

    @property
    def safe_data_dir(self) -> Path:
        return assert_safe_path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return assert_safe_path(self.safe_data_dir / "db" / "app.sqlite3")

    @property
    def profile_path(self) -> Path:
        return assert_safe_path(self.safe_data_dir / "profile" / "master_profile.json")

    @property
    def criteria_path(self) -> Path:
        return assert_safe_path(self.safe_data_dir / "config" / "search_criteria.json")

    @property
    def blocked_companies_path(self) -> Path:
        return assert_safe_path(self.safe_data_dir / "config" / "blocked_companies.json")

    @property
    def sources_path(self) -> Path:
        return assert_safe_path(self.safe_data_dir / "config" / "sources.json")

    @property
    def llm_settings_path(self) -> Path:
        return assert_safe_path(self.safe_data_dir / "config" / "llm_settings.json")

    @property
    def scheduler_config_path(self) -> Path:
        return assert_safe_path(self.safe_data_dir / "config" / "scheduler.json")

    @property
    def exports_dir(self) -> Path:
        return assert_safe_path(self.safe_data_dir / "exports")

    @property
    def backups_dir(self) -> Path:
        return assert_safe_path(self.safe_data_dir / "backups")


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()
