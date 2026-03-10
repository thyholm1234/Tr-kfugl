"""Scheduler til automatisk synkronisering."""

from __future__ import annotations

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import ScheduleConfig
from app.services.dofbasen import (
    fetch_and_store_species,
    fetch_all_phenology,
    fetch_and_store_observations,
    fetch_year_observations,
    fetch_historical_observations,
    build_migration_phenology,
)

logger = logging.getLogger(__name__)

SYNC_FUNCTIONS = {
    "species": fetch_and_store_species,
    "phenology": fetch_all_phenology,
    "observations": lambda s: fetch_and_store_observations(s, days=1),
    "year": fetch_year_observations,
    "historical": lambda s: fetch_historical_observations(s, years=5),
    "migration_phenology": build_migration_phenology,
}


async def _run_sync_job(sync_type: str) -> None:
    """Kør et sync-job i en ny session."""
    logger.info("Planlagt sync startet: %s", sync_type)
    fn = SYNC_FUNCTIONS.get(sync_type)
    if not fn:
        return
    try:
        async with async_session() as session:
            await fn(session)
        logger.info("Planlagt sync fuldført: %s", sync_type)
    except Exception:
        logger.exception("Fejl i planlagt sync: %s", sync_type)


def _parse_cron(expr: str) -> dict | None:
    """Parsér simpel cron-expression: 'minute hour day_of_week' eller 5-felt cron."""
    parts = expr.strip().split()
    if len(parts) == 5:
        return {
            "minute": parts[0],
            "hour": parts[1],
            "day": parts[2],
            "month": parts[3],
            "day_of_week": parts[4],
        }
    if len(parts) == 3:
        return {"minute": parts[0], "hour": parts[1], "day_of_week": parts[2]}
    return None


class SchedulerManager:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._started = False

    def is_running(self) -> bool:
        return self._started and self._scheduler.running

    async def start(self, db: AsyncSession) -> None:
        if not self._started:
            self._scheduler.start()
            self._started = True
        await self.reload_schedules(db)

    async def reload_schedules(self, db: AsyncSession) -> None:
        # Fjern alle eksisterende jobs
        for job in self._scheduler.get_jobs():
            job.remove()

        # Indlæs fra database
        result = await db.execute(select(ScheduleConfig).where(ScheduleConfig.enabled == True))
        for cfg in result.scalars().all():
            cron_kwargs = _parse_cron(cfg.cron_expression)
            if not cron_kwargs:
                logger.warning("Ugyldig cron for %s: %s", cfg.sync_type, cfg.cron_expression)
                continue
            try:
                trigger = CronTrigger(**cron_kwargs)
                self._scheduler.add_job(
                    _run_sync_job,
                    trigger=trigger,
                    args=[cfg.sync_type],
                    id=f"sync_{cfg.sync_type}",
                    replace_existing=True,
                )
                logger.info("Planlagt: %s med cron '%s'", cfg.sync_type, cfg.cron_expression)
            except Exception:
                logger.exception("Fejl ved oprettelse af job for %s", cfg.sync_type)

    def shutdown(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)


scheduler_manager = SchedulerManager()
