"""Analyse af trækaktivitet baseret på fænologi og aktuelle observationer.

Grundprincip:
  Fænologidata giver 36 ti-dages perioder med et gennemsnitligt antal
  individer pr. observation (ind/obs). Det totale areal under kurven er
  summen af alle 36 værdier. En arts "aktive sæson" er de perioder, der
  tilsammen rummer en betydelig del af arealet.

  For at finde trækfugle kigger vi på:
  1) Er arten i sin aktive sæson lige nu? (andel af areal i nuværende periode)
  2) Stiger kurven? (er vi på den stigende flanke = forårstrækket)
  3) Er arten netop ankommet? (lille areal i forrige periode, stort nu)
  4) Forventes arten snart? (stort areal i næste periode vs. nu)

  En art er en "target-art" hvis den nuværende periode ligger i artens
  kernesæson (de perioder der udgør ≥80% af arealet under kurven).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Observation, Phenology, Species


# ---------------------------------------------------------------------------
# Hjælpere
# ---------------------------------------------------------------------------

def current_period_index(d: datetime.date | None = None) -> int:
    """Returner 10-dages periodindeks (0-35) for en given dato."""
    if d is None:
        d = datetime.date.today()
    day_of_year = d.timetuple().tm_yday  # 1-366
    return min((day_of_year - 1) // 10, 35)


def period_to_date_label(period: int) -> str:
    """Giv en omtrentlig datolabel for en 10-dages periode."""
    start_day = period * 10 + 1
    d = datetime.date(2024, 1, 1) + datetime.timedelta(days=start_day - 1)
    end_d = d + datetime.timedelta(days=9)
    return f"{d.strftime('%d/%m')}–{end_d.strftime('%d/%m')}"


def _active_periods(values: list[float], threshold: float = 0.80) -> set[int]:
    """Find de perioder der tilsammen udgør ≥threshold af det totale areal.

    Sorterer perioderne efter værdi (højeste først) og akkumulerer
    indtil tærsklen er nået. Resultatet er de "vigtigste" perioder.
    """
    total = sum(v for v in values if v and v > 0)
    if total <= 0:
        return set()

    indexed = sorted(enumerate(values), key=lambda x: x[1] or 0, reverse=True)
    cumulative = 0.0
    active = set()
    for idx, val in indexed:
        if val is None or val <= 0:
            continue
        active.add(idx)
        cumulative += val
        if cumulative / total >= threshold:
            break
    return active


def _season_label(active: set[int], cur: int) -> str:
    """Fortæl om arten er i sæson, på vej ind, eller ude."""
    if not active:
        return "ukendt"
    prev = max(cur - 1, 0)
    nxt = min(cur + 1, 35)

    in_now = cur in active
    in_prev = prev in active
    in_next = nxt in active

    if in_now and not in_prev:
        return "netop ankommet"
    if in_now and in_next:
        return "i sæson"
    if in_now and not in_next:
        return "sæson slutter"
    if not in_now and in_next:
        return "forventes snart"
    return "ude af sæson"


# ---------------------------------------------------------------------------
# Dataklasser
# ---------------------------------------------------------------------------

@dataclass
class SpeciesInfo:
    euring: str
    artnavn: str
    latin: str
    english: str
    status: str

    # Fænologi
    phen_values: list[float] = field(default_factory=list)
    active_periods: set[int] = field(default_factory=set)
    total_area: float = 0.0
    current_period_value: float = 0.0
    current_period_pct: float = 0.0  # denne periodes andel af total
    current_year_value: float | None = None
    season_label: str = ""

    # Obs seneste 7 dage
    obs_count_7d: int = 0
    total_individuals_7d: int = 0
    migrating_obs_7d: int = 0

    # Årsdata
    year_obs_count: int = 0
    year_total_individuals: int = 0

    # Sammenligning
    year_comparison: str = ""


@dataclass
class Dashboard:
    period_index: int
    period_label: str
    next_period_label: str
    target_species: list[SpeciesInfo] = field(default_factory=list)
    coming_soon: list[SpeciesInfo] = field(default_factory=list)
    season_ending: list[SpeciesInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dashboard-bygger
# ---------------------------------------------------------------------------

async def build_dashboard(session: AsyncSession) -> Dashboard:
    today = datetime.date.today()
    pi = current_period_index(today)
    next_pi = min(pi + 1, 35)
    seven_days_ago = today - datetime.timedelta(days=7)

    dashboard = Dashboard(
        period_index=pi,
        period_label=period_to_date_label(pi),
        next_period_label=period_to_date_label(next_pi),
    )

    # Alle arter (A og SU), kun rene arter (ikke hybrider/underarter)
    sp_q = await session.execute(
        select(Species).where(
            Species.status.in_(["A", "SU"]),
            Species.art_type == "art",
        ).order_by(Species.sortering)
    )
    all_species = {s.euring: s for s in sp_q.scalars().all()}
    if not all_species:
        return dashboard

    # Hent ALLE fænologidata
    phen_q = await session.execute(
        select(Phenology).order_by(Phenology.euring, Phenology.period_index)
    )
    phen_map: dict[str, list[float]] = {}
    phen_cy_map: dict[str, list[float | None]] = {}
    for p in phen_q.scalars().all():
        phen_map.setdefault(p.euring, [0.0] * 36)
        phen_cy_map.setdefault(p.euring, [None] * 36)
        if 0 <= p.period_index < 36:
            phen_map[p.euring][p.period_index] = p.avg_value or 0.0
            phen_cy_map[p.euring][p.period_index] = p.current_year_value

    # Obs seneste 7 dage
    obs_q = await session.execute(
        select(
            Observation.artnr,
            func.count(Observation.id).label("cnt"),
            func.coalesce(func.sum(Observation.antal), 0).label("ind"),
            func.sum(case((Observation.adfkode == "T", 1), else_=0)).label("mig"),
        )
        .where(Observation.dato >= seven_days_ago)
        .group_by(Observation.artnr)
    )
    obs_map = {r.artnr: (r.cnt, r.ind, r.mig or 0) for r in obs_q.all()}

    # Årets data
    year_start = datetime.date(today.year, 1, 1)
    yr_q = await session.execute(
        select(
            Observation.artnr,
            func.count(Observation.id).label("cnt"),
            func.coalesce(func.sum(Observation.antal), 0).label("ind"),
        )
        .where(Observation.dato >= year_start)
        .group_by(Observation.artnr)
    )
    yr_map = {r.artnr: (r.cnt, r.ind) for r in yr_q.all()}

    # Byg info for alle arter med fænologi
    targets = []
    coming = []
    ending = []

    for euring, sp in all_species.items():
        vals = phen_map.get(euring)
        if not vals:
            continue

        total = sum(v for v in vals if v > 0)
        if total <= 0:
            continue

        active = _active_periods(vals)
        label = _season_label(active, pi)
        cur_val = vals[pi]
        cur_pct = (cur_val / total * 100) if total > 0 else 0
        cy_vals = phen_cy_map.get(euring, [None] * 36)
        cy_val = cy_vals[pi] if pi < len(cy_vals) else None

        info = SpeciesInfo(
            euring=euring,
            artnavn=sp.artnavn,
            latin=sp.latin,
            english=sp.english,
            status=sp.status,
            phen_values=vals,
            active_periods=active,
            total_area=total,
            current_period_value=cur_val,
            current_period_pct=round(cur_pct, 1),
            current_year_value=cy_val,
            season_label=label,
        )

        obs = obs_map.get(euring, (0, 0, 0))
        info.obs_count_7d = obs[0]
        info.total_individuals_7d = obs[1]
        info.migrating_obs_7d = obs[2]

        yr = yr_map.get(euring, (0, 0))
        info.year_obs_count = yr[0]
        info.year_total_individuals = yr[1]

        if cy_val is not None and cur_val > 0.01:
            ratio = cy_val / cur_val
            if ratio > 1.3:
                info.year_comparison = "over gns."
            elif ratio < 0.7:
                info.year_comparison = "under gns."
            else:
                info.year_comparison = "normalt"

        if label in ("i sæson", "netop ankommet"):
            targets.append(info)
        elif label == "forventes snart":
            coming.append(info)
        elif label == "sæson slutter":
            ending.append(info)

    dashboard.target_species = sorted(
        targets, key=lambda x: x.current_period_pct, reverse=True
    )
    dashboard.coming_soon = sorted(
        coming, key=lambda x: (x.phen_values[next_pi] if next_pi < 36 else 0), reverse=True
    )
    dashboard.season_ending = sorted(
        ending, key=lambda x: x.current_period_pct, reverse=True
    )

    return dashboard


# ---------------------------------------------------------------------------
# Artsdetalje
# ---------------------------------------------------------------------------

async def get_species_year_data(
    session: AsyncSession, euring: str
) -> dict[str, Any]:
    sp = await session.execute(
        select(Species).where(Species.euring == euring)
    )
    species = sp.scalar_one_or_none()
    if not species:
        return {}

    phen_q = await session.execute(
        select(Phenology)
        .where(Phenology.euring == euring)
        .order_by(Phenology.period_index)
    )
    phenology = phen_q.scalars().all()

    today = datetime.date.today()
    year_start = datetime.date(today.year, 1, 1)
    obs_daily = await session.execute(
        select(
            Observation.dato,
            func.count(Observation.id).label("obs_count"),
            func.coalesce(func.sum(Observation.antal), 0).label("total_ind"),
        )
        .where(Observation.artnr == euring, Observation.dato >= year_start)
        .group_by(Observation.dato)
        .order_by(Observation.dato)
    )

    daily_data = [
        {"dato": row.dato.isoformat(), "obs_count": row.obs_count, "total_ind": row.total_ind}
        for row in obs_daily.all()
    ]

    phen_data = [
        {
            "period": p.period_index,
            "label": period_to_date_label(p.period_index),
            "avg": round(p.avg_value, 4),
            "last_year": round(p.last_year_value, 4) if p.last_year_value else None,
            "current_year": round(p.current_year_value, 4) if p.current_year_value else None,
        }
        for p in phenology
    ]

    vals = [p.avg_value or 0.0 for p in phenology] if phenology else []
    active = _active_periods(vals) if vals else set()
    pi = current_period_index(today)

    return {
        "species": {
            "euring": species.euring,
            "artnavn": species.artnavn,
            "latin": species.latin,
            "english": species.english,
            "status": species.status,
        },
        "phenology": phen_data,
        "daily_observations": daily_data,
        "current_period": pi,
        "active_periods": sorted(active),
        "season_label": _season_label(active, pi),
        "image_url": f"https://dofbasen.dk/danmarksfugle/art/{euring}",
    }
