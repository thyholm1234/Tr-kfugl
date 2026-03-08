"""Trækfugl – FastAPI app."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import engine, Base, async_session
from app.routers.views import router
from sqlalchemy import select

from app.models import DataSync
from app.services.dofbasen import (
    fetch_and_store_species,
    fetch_all_phenology,
    fetch_and_store_observations,
    fetch_year_observations,
)
from app.services.scheduler import scheduler_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def initial_sync() -> None:
    """Kør data-synkronisering i baggrunden – kun det der mangler."""
    await asyncio.sleep(5)  # lad serveren starte
    async with async_session() as session:
        # Check hvad der allerede er synkroniseret
        syncs = await session.execute(select(DataSync.sync_type))
        existing = {r[0] for r in syncs.all()}

        if "species" not in existing:
            try:
                logger.info("Synkroniserer artsliste (første gang) …")
                await fetch_and_store_species(session)
            except Exception:
                logger.exception("Fejl ved synk af artsliste")
        else:
            logger.info("Artsliste allerede synkroniseret – springer over.")

        if "phenology" not in existing:
            try:
                logger.info("Synkroniserer fænologidata (første gang) …")
                await fetch_all_phenology(session)
            except Exception:
                logger.exception("Fejl ved synk af fænologi")
        else:
            logger.info("Fænologidata allerede synkroniseret – springer over.")

        if "observations" not in existing:
            try:
                logger.info("Henter observationer (seneste 7 dage) …")
                await fetch_and_store_observations(session, days=7)
            except Exception:
                logger.exception("Fejl ved synk af observationer")
            try:
                logger.info("Henter årets observationer …")
                await fetch_year_observations(session)
            except Exception:
                logger.exception("Fejl ved synk af årets observationer")
        else:
            logger.info("Observationer allerede synkroniseret – springer over.")

        # Start scheduler
        await scheduler_manager.start(session)

    logger.info("Opstartscheck fuldført.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Opret tabeller
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database-tabeller oprettet.")

    # Start baggrundssynkronisering
    task = asyncio.create_task(initial_sync())
    yield
    task.cancel()
    scheduler_manager.shutdown()


app = FastAPI(
    title="Trækfugl",
    description="Fugletræk-dashboard for Danmark",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
