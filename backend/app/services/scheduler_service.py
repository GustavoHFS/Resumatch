"""APScheduler manager integrated into FastAPI lifespan (disabled by default)."""

from __future__ import annotations

import asyncio
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Engine

from app.core.config import AppSettings
from app.services.discovery_service import execute_discovery_run

JOB_ID = "periodic_public_discovery"


class DiscoverySchedulerManager:
    """Controls background periodic discovery without overlapping manual runs."""

    def __init__(self, db_engine: Engine, settings: AppSettings) -> None:
        self.db_engine = db_engine
        self.settings = settings
        self.run_lock = asyncio.Lock()
        self.enabled: bool = bool(settings.scheduler_enabled)
        self.interval_minutes: int = max(15, int(settings.scheduler_default_interval_minutes))
        self.max_requests_budget: int = 40
        self.last_run_summary: dict[str, Any] | None = None
        self._scheduler = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        self._sync_job()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def _sync_job(self) -> None:
        existing = self._scheduler.get_job(JOB_ID)
        if not self.enabled:
            if existing:
                self._scheduler.remove_job(JOB_ID)
            return

        trigger = IntervalTrigger(minutes=self.interval_minutes)
        if existing:
            self._scheduler.reschedule_job(JOB_ID, trigger=trigger)
        else:
            self._scheduler.add_job(
                self._scheduled_tick,
                trigger=trigger,
                id=JOB_ID,
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )

    async def _scheduled_tick(self) -> None:
        if self.run_lock.locked():
            return
        async with self.run_lock:
            self.last_run_summary = await execute_discovery_run(
                db_engine=self.db_engine,
                settings=self.settings,
                max_requests_budget=self.max_requests_budget,
            )

    def configure(
        self,
        *,
        enabled: bool,
        interval_minutes: int | None = None,
        max_requests_budget: int | None = None,
    ) -> dict[str, Any]:
        self.enabled = enabled
        if interval_minutes is not None:
            if not 15 <= interval_minutes <= 10080:
                raise ValueError("interval_minutes must be between 15 and 10080")
            self.interval_minutes = interval_minutes
        if max_requests_budget is not None:
            if not 5 <= max_requests_budget <= 150:
                raise ValueError("max_requests_budget must be between 5 and 150")
            self.max_requests_budget = max_requests_budget
        self._sync_job()
        return self.status()

    def status(self) -> dict[str, Any]:
        job = self._scheduler.get_job(JOB_ID) if self._scheduler.running else None
        next_run = (
            job.next_run_time.isoformat()
            if job and getattr(job, "next_run_time", None) is not None
            else None
        )
        return {
            "scheduler_running": self._scheduler.running,
            "enabled": self.enabled,
            "is_executing": self.run_lock.locked(),
            "interval_minutes": self.interval_minutes,
            "max_requests_budget": self.max_requests_budget,
            "next_run_time": next_run,
            "last_run_id": (self.last_run_summary or {}).get("id"),
        }
