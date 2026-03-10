"""Analyse af trækaktivitet baseret på fænologi og aktuelle observationer.

Grundprincip:
  Fænologidata giver 36 ti-dages perioder med et gennemsnitligt antal
  individer pr. observation (ind/obs). Det totale areal under kurven er
  summen af alle 36 værdier.

  TRÆKFUGLE-KLASSIFIKATION (baseret på fænologisk seasonality):
  Vi bruger et "seasonality index" til at skelne mellem trækfugle,
  delvise trækfugle og standfugle:

  1. Beregn artens "tilstedeværelsesratio" – andelen af 36 perioder
     hvor arten forekommer i ≥5% af sit peak-niveau.
  2. Klassificér:
     - Trækfugl:         tilstedeværelse < 50% af året
     - Delvis trækfugl:  tilstedeværelse 50–75% af året
     - Standfugl:         tilstedeværelse > 75% af året

  ANKOMST vs. AFGANG:
  For trækfugle bestemmer vi retning fra fænologikurvens hældning:
  - Stigende kurve (nuværende > forrige) → ankomst (forårstræk)
  - Faldende kurve (nuværende < næste) → afgang (efterårstræk)
  - Peak-nært → gennemtræk/tilstede

  Standfugle vises separat med deres sæsonmæssige aktivitetsniveau.
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


def _classify_migration(values: list[float]) -> str:
    """Klassificér art som trækfugl, delvis trækfugl eller standfugl.

    Baseret på tilstedeværelsesratio: andelen af 36 perioder hvor
    arten forekommer i ≥5% af sit peak-niveau.
    - < 50% → trækfugl
    - 50–75% → delvis trækfugl
    - > 75% → standfugl
    """
    peak = max((v for v in values if v), default=0)
    if peak <= 0:
        return "ukendt"

    threshold = peak * 0.05
    present_count = sum(1 for v in values if v and v >= threshold)
    presence_ratio = present_count / 36

    if presence_ratio < 0.50:
        return "trækfugl"
    elif presence_ratio < 0.75:
        return "delvis trækfugl"
    else:
        return "standfugl"


def _migration_direction(values: list[float], cur: int) -> str:
    """Bestem trækretning fra fænologikurvens hældning.

    Returns: 'ankomst', 'afgang', 'peak' eller 'ude af sæson'.
    """
    cur_val = values[cur] if 0 <= cur < 36 else 0
    prev_val = values[cur - 1] if cur > 0 else 0
    next_val = values[cur + 1] if cur < 35 else 0

    # Ikke tilstede nu
    peak = max((v for v in values if v), default=0)
    if peak <= 0 or (cur_val or 0) < peak * 0.02:
        return "ude af sæson"

    rising = (cur_val or 0) > (prev_val or 0) * 1.2
    falling = (next_val or 0) < (cur_val or 0) * 0.8

    if rising and not falling:
        return "ankomst"
    elif falling and not rising:
        return "afgang"
    elif rising and falling:
        return "gennemtræk"
    else:
        return "tilstede"


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

    # Trækklassifikation
    migration_type: str = ""  # trækfugl / delvis trækfugl / standfugl
    migration_direction: str = ""  # ankomst / afgang / gennemtræk / tilstede / ude af sæson

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
    # Trækfugle
    arrivals: list[SpeciesInfo] = field(default_factory=list)       # ankomster nu
    departures: list[SpeciesInfo] = field(default_factory=list)     # afgange nu
    passing_through: list[SpeciesInfo] = field(default_factory=list)  # gennemtræk/tilstede
    coming_soon: list[SpeciesInfo] = field(default_factory=list)    # forventes snart
    # Standfugle
    residents_active: list[SpeciesInfo] = field(default_factory=list)  # standfugle i peak
    # Legacy (for API compatibility)
    target_species: list[SpeciesInfo] = field(default_factory=list)
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
    arrivals = []
    departures = []
    passing = []
    coming = []
    residents = []

    for euring, sp in all_species.items():
        vals = phen_map.get(euring)
        if not vals:
            continue

        total = sum(v for v in vals if v > 0)
        if total <= 0:
            continue

        active = _active_periods(vals)
        label = _season_label(active, pi)
        mig_type = _classify_migration(vals)
        mig_dir = _migration_direction(vals, pi)
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
            migration_type=mig_type,
            migration_direction=mig_dir,
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

        # Klassificér i dashboard-sektioner
        in_season = label in ("i sæson", "netop ankommet", "sæson slutter")

        if mig_type == "standfugl":
            # Standfugle: vis kun hvis de er i deres kernesæson
            if in_season:
                residents.append(info)
        else:
            # Trækfugle og delvise trækfugle
            if label == "forventes snart":
                coming.append(info)
            elif in_season:
                if mig_dir == "ankomst":
                    arrivals.append(info)
                elif mig_dir == "afgang":
                    departures.append(info)
                else:
                    passing.append(info)

    sort_key = lambda x: x.current_period_pct
    dashboard.arrivals = sorted(arrivals, key=sort_key, reverse=True)
    dashboard.departures = sorted(departures, key=sort_key, reverse=True)
    dashboard.passing_through = sorted(passing, key=sort_key, reverse=True)
    dashboard.coming_soon = sorted(
        coming, key=lambda x: (x.phen_values[next_pi] if next_pi < 36 else 0), reverse=True
    )
    dashboard.residents_active = sorted(residents, key=sort_key, reverse=True)

    # Legacy: target_species = alle trækfugle i sæson
    dashboard.target_species = sorted(
        arrivals + departures + passing, key=sort_key, reverse=True
    )
    dashboard.season_ending = [
        s for s in dashboard.target_species if s.season_label == "sæson slutter"
    ]

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
    mig_type = _classify_migration(vals) if vals else "ukendt"
    mig_dir = _migration_direction(vals, pi) if vals else "ukendt"

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
        "migration_type": mig_type,
        "migration_direction": mig_dir,
        "image_url": f"https://dofbasen.dk/danmarksfugle/art/{euring}",
    }
