"""Henter data fra DOFbasen og gemmer i databasen."""

from __future__ import annotations

import asyncio
import csv
import io
import datetime
import logging
from typing import Any

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    DOFBASEN_SPECIES_URL,
    DOFBASEN_PHENOLOGY_URL,
    DOFBASEN_OBSERVATIONS_URL,
)
from app.models import Species, Phenology, Observation, DataSync

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Traekfugl/1.0 (fugletraek-dashboard)"}


# ---------------------------------------------------------------------------
# Artsliste
# ---------------------------------------------------------------------------

async def fetch_and_store_species(session: AsyncSession) -> int:
    """Hent artsliste-CSV fra DOFbasen og upsert i databasen."""
    async with httpx.AsyncClient(timeout=60, headers=HEADERS) as client:
        resp = await client.get(DOFBASEN_SPECIES_URL)
        resp.raise_for_status()

    text_body = resp.content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text_body), delimiter=";")

    count = 0
    for row in reader:
        artnavn = row.get("Artnavn", "").strip().strip('"')
        latin = row.get("Latin", "").strip().strip('"')
        english = row.get("English", "").strip().strip('"')
        euring = row.get("Euring", "").strip()
        if not euring:
            continue

        stmt = pg_insert(Species).values(
            euring=euring,
            sortering=int(row.get("Sortering", 0)),
            artnavn=artnavn,
            latin=latin,
            english=english,
            status=row.get("Status", "").strip(),
            art_type=row.get("Type", "").strip(),
        ).on_conflict_do_update(
            index_elements=["euring"],
            set_=dict(
                artnavn=artnavn,
                latin=latin,
                english=english,
                status=row.get("Status", "").strip(),
                art_type=row.get("Type", "").strip(),
            ),
        )
        await session.execute(stmt)
        count += 1

    await _update_sync(session, "species")
    await session.commit()
    logger.info("Synkroniserede %d arter", count)
    return count


# ---------------------------------------------------------------------------
# Fænologi
# ---------------------------------------------------------------------------

async def fetch_and_store_phenology(
    session: AsyncSession, euring: str
) -> bool:
    """Hent fænologidata for én art og gem 36 10-dages perioder."""
    url = f"{DOFBASEN_PHENOLOGY_URL}?artnr={euring}"
    async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return False
        data: dict[str, Any] = resp.json()

    avg = data.get("average", {})
    avg_values: list[float] = avg.get("averageValues", [])
    first_year = avg.get("firstYear")
    last_year_data = data.get("lastYear", {})
    last_year_values: list[float] = last_year_data.get("values", [])
    current_year_data = data.get("currentYear", {})
    current_year_values: list[float] = current_year_data.get("values", [])

    if not avg_values:
        return False

    for i, val in enumerate(avg_values):
        if val is None:
            val = 0.0
        ly = last_year_values[i] if i < len(last_year_values) else None
        cy = current_year_values[i] if i < len(current_year_values) else None

        stmt = pg_insert(Phenology).values(
            euring=euring,
            period_index=i,
            avg_value=val,
            last_year_value=ly,
            current_year_value=cy,
            avg_first_year=first_year,
            avg_last_year=avg.get("lastYear"),
        ).on_conflict_do_update(
            constraint="uq_phenology_euring_period_index",
            set_=dict(
                avg_value=val,
                last_year_value=ly,
                current_year_value=cy,
                avg_first_year=first_year,
                avg_last_year=avg.get("lastYear"),
            ),
        )
        await session.execute(stmt)

    await session.commit()
    return True


async def fetch_all_phenology(session: AsyncSession) -> int:
    """Hent fænologi for alle almindelige arter (status A)."""
    result = await session.execute(
        select(Species.euring).where(Species.status == "A")
    )
    eurings = [r[0] for r in result.all()]
    count = 0
    for euring in eurings:
        try:
            ok = await fetch_and_store_phenology(session, euring)
            if ok:
                count += 1
        except Exception:
            logger.exception("Fænologi fejl for %s", euring)
            await session.rollback()
        await asyncio.sleep(10)  # rate limit
    await _update_sync(session, "phenology")
    await session.commit()
    logger.info("Synkroniserede fænologi for %d arter", count)
    return count


# ---------------------------------------------------------------------------
# Observationer
# ---------------------------------------------------------------------------

def _build_obs_url(days: int) -> str:
    return (
        "https://dofbasen.dk/excel/search_result1.php"
        f"?design=excel&soeg=soeg&periode=antaldage&dage={days}"
        "&obstype=observationer&species=alle&sortering=dato"
    )


def _build_obs_url_dates(date_from: str, date_to: str) -> str:
    return (
        "https://dofbasen.dk/excel/search_result1.php"
        f"?design=excel&soeg=soeg&periode=dato"
        f"&dato_fra={date_from}&dato_til={date_to}"
        "&obstype=observationer&species=alle&sortering=dato"
    )


async def fetch_and_store_observations(
    session: AsyncSession, days: int = 7
) -> int:
    """Hent observationer for de seneste N dage."""
    url = _build_obs_url(days)
    return await _fetch_obs_from_url(session, url)


async def fetch_observations_date_range(
    session: AsyncSession, date_from: str, date_to: str
) -> int:
    """Hent observationer for en given datoperiode (YYYY-MM-DD)."""
    url = _build_obs_url_dates(date_from, date_to)
    return await _fetch_obs_from_url(session, url)


async def _fetch_obs_from_url(session: AsyncSession, url: str) -> int:
    async with httpx.AsyncClient(timeout=300, headers=HEADERS) as client:
        for attempt in range(3):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                break
            except (httpx.RemoteProtocolError, httpx.ReadTimeout) as exc:
                if attempt == 2:
                    raise
                logger.warning("Retry %d for %s: %s", attempt + 1, url[:80], exc)
                await asyncio.sleep(10 * (attempt + 1))

    text_body = resp.content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text_body), delimiter=";")

    count = 0
    for row in reader:
        obsid_raw = row.get("Obsid", "").strip()
        if not obsid_raw:
            continue
        try:
            obsid = int(obsid_raw)
        except ValueError:
            continue

        dato_str = row.get("Dato", "").strip()
        if not dato_str:
            continue
        try:
            dato = datetime.date.fromisoformat(dato_str)
        except ValueError:
            continue

        antal_raw = row.get("Antal", "").strip()
        try:
            antal = int(antal_raw) if antal_raw else None
        except ValueError:
            antal = None

        values = dict(
            dato=dato,
            turtidfra=row.get("Turtidfra", "").strip() or None,
            turtidtil=row.get("Turtidtil", "").strip() or None,
            loknr=row.get("Loknr", "").strip() or None,
            loknavn=_clean(row.get("Loknavn")),
            artnr=row.get("Artnr", "").strip(),
            artnavn=_clean(row.get("Artnavn")),
            latin=_clean(row.get("Latin")),
            sortering=_int_or_none(row.get("Sortering")),
            antal=antal,
            koen=row.get("Koen", "").strip() or None,
            adfkode=row.get("Adfkode", "").strip() or None,
            adfbeskrivelse=_clean(row.get("Adfbeskrivelse")),
            alderkode=row.get("Alderkode", "").strip() or None,
            dragtkode=row.get("Dragtkode", "").strip() or None,
            dragtbeskrivelse=_clean(row.get("Dragtbeskrivelse")),
            obserkode=row.get("Obserkode", "").strip() or None,
            fornavn=_clean(row.get("Fornavn")),
            efternavn=_clean(row.get("Efternavn")),
            obser_by=_clean(row.get("Obser_by")),
            medobser=_clean(row.get("Medobser")),
            turnoter=_clean(row.get("Turnoter")),
            fuglnoter=_clean(row.get("Fuglnoter")),
            metode=_clean(row.get("Metode")),
            obstidfra=row.get("Obstidfra", "").strip() or None,
            obstidtil=row.get("Obstidtil", "").strip() or None,
            hemmelig=_int_or_none(row.get("Hemmelig")),
            kvalitet=_int_or_none(row.get("Kvalitet")),
            turid=_int_or_none(row.get("Turid")),
            obsid=obsid,
            dof_afdeling=_clean(row.get("DOF_afdeling")),
            lok_laengdegrad=row.get("lok_laengdegrad", "").strip() or None,
            lok_breddegrad=row.get("lok_breddegrad", "").strip() or None,
            obs_laengdegrad=row.get("obs_laengdegrad", "").strip() or None,
            obs_breddegrad=row.get("obs_breddegrad", "").strip() or None,
            radius=row.get("radius", "").strip() or None,
            obser_laengdegrad=row.get("obser_laengdegrad", "").strip() or None,
            obser_breddegrad=row.get("obser_breddegrad", "").strip() or None,
        )

        stmt = pg_insert(Observation).values(**values).on_conflict_do_update(
            index_elements=["obsid"],
            set_={k: v for k, v in values.items() if k != "obsid"},
        )
        await session.execute(stmt)
        count += 1

        if count % 2000 == 0:
            await session.commit()

    await _update_sync(session, "observations")
    await session.commit()
    logger.info("Synkroniserede %d observationer", count)
    return count


# ---------------------------------------------------------------------------
# Årets observationer (månedsvis)
# ---------------------------------------------------------------------------

async def fetch_year_observations(session: AsyncSession) -> int:
    """Hent observationer for hele indeværende år i 10-dages bidder."""
    today = datetime.date.today()
    total = 0
    start = datetime.date(today.year, 1, 1)
    while start <= today:
        end = min(start + datetime.timedelta(days=9), today)
        try:
            n = await fetch_observations_date_range(
                session,
                start.isoformat(),
                end.isoformat(),
            )
            total += n
            logger.info("Periode %s – %s: %d obs", start, end, n)
        except Exception:
            logger.exception("Fejl ved hentning af %s – %s", start, end)
        await asyncio.sleep(10)  # rate limit
        start = end + datetime.timedelta(days=1)
    await _update_sync(session, "year")
    await session.commit()
    return total


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(val: str | None) -> str | None:
    if val is None:
        return None
    v = val.strip().strip('"')
    return v if v and v != "-" else None


def _int_or_none(val: str | None) -> int | None:
    if not val:
        return None
    val = val.strip()
    try:
        return int(val)
    except ValueError:
        return None


async def _update_sync(session: AsyncSession, sync_type: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    stmt = pg_insert(DataSync).values(
        sync_type=sync_type,
        last_sync=now,
    ).on_conflict_do_update(
        index_elements=["sync_type"],
        set_=dict(last_sync=now),
    )
    await session.execute(stmt)
