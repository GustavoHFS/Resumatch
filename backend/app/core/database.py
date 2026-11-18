"""SQLite WAL database engine, pragma tuning, and SQLModel session management."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import Engine, event, text
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import assert_safe_path


def create_sqlite_wal_engine(db_path: Path) -> Engine:
    safe_db = assert_safe_path(db_path)
    safe_db.parent.mkdir(parents=True, exist_ok=True)
    sqlite_url = f"sqlite:///{safe_db.as_posix()}"

    engine = create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False, "timeout": 15},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    return engine


def init_db_schema(engine: Engine) -> str:
    # Ensure all relational models are imported before metadata.create_all
    import app.models.entities  # noqa: F401

    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        journal_mode = str(conn.execute(text("PRAGMA journal_mode")).scalar() or "").lower()
    return journal_mode


def get_db_session(request: Request) -> Generator[Session, None, None]:
    engine: Engine = request.app.state.engine
    with Session(engine) as session:
        yield session
