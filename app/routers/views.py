"""Endpoints for views og API."""

from __future__ import annotations

import asyncio
import datetime
import math
import re

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DMI_CLIMATE_BASE
from app.database import get_db, async_session
from app.models import DataSync, Observation, ScheduleConfig, Species
from app.services.dofbasen import (
    fetch_and_store_species,
    fetch_all_phenology,
    fetch_and_store_observations,
    fetch_year_observations,
    fetch_historical_observations,
    build_migration_phenology,
)
from app.services.migration_analysis import (
    build_dashboard,
    get_species_year_data,
    current_period_index,
    period_to_date_label,
)
from app.services.scheduler import scheduler_manager

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Forside
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    dashboard = await build_dashboard(db)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "dashboard": dashboard,
            "today": datetime.date.today().isoformat(),
            "current_period": current_period_index(),
        },
    )


# ---------------------------------------------------------------------------
# Artsdetalje
# ---------------------------------------------------------------------------

@router.get("/art/{euring}", response_class=HTMLResponse)
async def species_detail(
    request: Request, euring: str, db: AsyncSession = Depends(get_db)
):
    data = await get_species_year_data(db, euring)
    if not data:
        return HTMLResponse("<h1>Art ikke fundet</h1>", status_code=404)

    return templates.TemplateResponse(
        "species.html",
        {
            "request": request,
            "data": data,
            "today": datetime.date.today().isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, db: AsyncSession = Depends(get_db)):
    sync_q = await db.execute(select(DataSync))
    syncs = {
        s.sync_type: s.last_sync.strftime("%Y-%m-%d %H:%M")
        for s in sync_q.scalars().all()
    }

    sched_q = await db.execute(select(ScheduleConfig))
    schedules = {
        s.sync_type: {"cron": s.cron_expression, "enabled": s.enabled}
        for s in sched_q.scalars().all()
    }

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "syncs": syncs,
            "schedules": schedules,
            "scheduler_running": scheduler_manager.is_running(),
        },
    )


@router.post("/admin/schedule")
async def update_schedule(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    sync_type = str(form.get("sync_type", ""))
    cron_expr = str(form.get("cron_expression", "")).strip()
    enabled = form.get("enabled") == "on"

    if sync_type not in ("species", "phenology", "observations", "year", "historical", "migration_phenology"):
        return JSONResponse({"error": "invalid sync_type"}, status_code=400)

    existing = await db.execute(
        select(ScheduleConfig).where(ScheduleConfig.sync_type == sync_type)
    )
    sched = existing.scalar_one_or_none()
    if sched:
        sched.cron_expression = cron_expr
        sched.enabled = enabled
    else:
        db.add(ScheduleConfig(
            sync_type=sync_type, cron_expression=cron_expr, enabled=enabled,
        ))
    await db.commit()

    # Genindlæs scheduler
    await scheduler_manager.reload_schedules(db)

    return JSONResponse({"status": "ok", "sync_type": sync_type})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/api/species")
async def api_species(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Species).where(Species.status.in_(["A", "SU"])).order_by(Species.sortering)
    )
    return [
        {"euring": s.euring, "artnavn": s.artnavn, "latin": s.latin, "english": s.english, "status": s.status}
        for s in result.scalars().all()
    ]


@router.get("/api/dashboard")
async def api_dashboard(db: AsyncSession = Depends(get_db)):
    dashboard = await build_dashboard(db)

    def _serialize(info):
        return {
            "euring": info.euring,
            "artnavn": info.artnavn,
            "latin": info.latin,
            "status": info.status,
            "obs_count_7d": info.obs_count_7d,
            "total_individuals_7d": info.total_individuals_7d,
            "season_label": info.season_label,
            "current_period_pct": info.current_period_pct,
            "year_comparison": info.year_comparison,
            "migration_type": info.migration_type,
            "migration_direction": info.migration_direction,
        }

    return {
        "period": dashboard.period_label,
        "next_period": dashboard.next_period_label,
        "arrivals": [_serialize(i) for i in dashboard.arrivals],
        "departures": [_serialize(i) for i in dashboard.departures],
        "passing_through": [_serialize(i) for i in dashboard.passing_through],
        "coming_soon": [_serialize(i) for i in dashboard.coming_soon],
        "residents_active": [_serialize(i) for i in dashboard.residents_active],
        # Legacy
        "target_species": [_serialize(i) for i in dashboard.target_species],
        "season_ending": [_serialize(i) for i in dashboard.season_ending],
    }


@router.get("/api/art/{euring}")
async def api_species_detail(euring: str, db: AsyncSession = Depends(get_db)):
    return await get_species_year_data(db, euring)


# ---------------------------------------------------------------------------
# Kort (map)
# ---------------------------------------------------------------------------

# Kompas-retninger til grader (0 = nord, clockwise)
_DIR_DEGREES: dict[str, float] = {
    "N": 0, "NNØ": 22.5, "NØ": 45, "ØNØ": 67.5,
    "Ø": 90, "ØSØ": 112.5, "SØ": 135, "SSØ": 157.5,
    "S": 180, "SSV": 202.5, "SV": 225, "VSV": 247.5,
    "V": 270, "VNV": 292.5, "NV": 315, "NNV": 337.5,
    # Engelske aliaser
    "E": 90, "NE": 45, "SE": 135, "SW": 225, "NW": 315, "W": 270,
}

# Regex der fanger retning fra fuglnoter
_DIR_PATTERN = re.compile(
    r"(?:^|[\s,.(])"
    r"(NNØ|NØ|ØNØ|ØSØ|SØ|SSØ|SSV|SV|VSV|VNV|NV|NNV|NE|NW|SE|SW|[NSØVE])"
    r"(?:$|[\s,.)!?])",
    re.IGNORECASE,
)
_DIR_WORD = re.compile(
    r"(nord|syd|øst|vest)(træk|gående|flyvende)?",
    re.IGNORECASE,
)


def _parse_direction(notes: str | None) -> float | None:
    """Forsøg at parse en trækretning (grader) fra fuglnoter."""
    if not notes:
        return None
    m = _DIR_PATTERN.search(notes)
    if m:
        return _DIR_DEGREES.get(m.group(1).upper())
    m = _DIR_WORD.search(notes)
    if m:
        word = m.group(1).lower()
        return {"nord": 0, "syd": 180, "øst": 90, "vest": 270}.get(word)
    return None


def _parse_coord(val: str | None) -> float | None:
    """Konverter dansk decimaltal (komma) til float."""
    if not val:
        return None
    try:
        return float(val.replace(",", "."))
    except (ValueError, TypeError):
        return None


@router.get("/kort", response_class=HTMLResponse)
async def map_page(request: Request, db: AsyncSession = Depends(get_db)):
    # Artsliste til filter-dropdown
    sp_q = await db.execute(
        select(Species.euring, Species.artnavn)
        .where(Species.status.in_(["A", "SU"]), Species.art_type == "art")
        .order_by(Species.sortering)
    )
    species_list = [{"euring": r.euring, "artnavn": r.artnavn} for r in sp_q.all()]

    return templates.TemplateResponse(
        "map.html",
        {
            "request": request,
            "species_list": species_list,
        },
    )


@router.get("/api/map/migrations")
async def api_map_migrations(
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=7, ge=1, le=365),
    euring: str | None = Query(default=None),
    only_migrating: bool = Query(default=True),
):
    """Trækkende observationer aggregeret pr. lokalitet, med retning."""
    since = datetime.date.today() - datetime.timedelta(days=days)

    q = (
        select(
            Observation.loknr,
            Observation.loknavn,
            Observation.lok_laengdegrad,
            Observation.lok_breddegrad,
            Observation.artnavn,
            Observation.artnr,
            Observation.antal,
            Observation.fuglnoter,
            Observation.dato,
        )
        .where(
            Observation.dato >= since,
            Observation.lok_laengdegrad.isnot(None),
            Observation.lok_laengdegrad != "",
        )
    )
    if only_migrating:
        q = q.where(Observation.adfkode == "T")
    if euring:
        q = q.where(Observation.artnr == euring)

    result = await db.execute(q.order_by(Observation.dato.desc()))
    rows = result.all()

    # Aggreger pr. lokalitet
    loc_map: dict[str, dict] = {}
    for r in rows:
        lng = _parse_coord(r.lok_laengdegrad)
        lat = _parse_coord(r.lok_breddegrad)
        if lng is None or lat is None:
            continue
        key = r.loknr or f"{lat:.4f},{lng:.4f}"
        if key not in loc_map:
            loc_map[key] = {
                "loknavn": r.loknavn or "Ukendt",
                "lat": lat,
                "lng": lng,
                "total": 0,
                "species": {},
                "directions": [],
            }
        loc = loc_map[key]
        antal = r.antal or 1
        loc["total"] += antal
        sp_name = r.artnavn or r.artnr
        loc["species"][sp_name] = loc["species"].get(sp_name, 0) + antal

        direction = _parse_direction(r.fuglnoter)
        if direction is not None:
            loc["directions"].append((direction, antal))

    features = []
    for loc in loc_map.values():
        # Vægtet cirkulært gennemsnit (vægt = antal individer)
        avg_dir = None
        directed_count = 0
        if loc["directions"]:
            sin_sum = sum(w * math.sin(math.radians(d)) for d, w in loc["directions"])
            cos_sum = sum(w * math.cos(math.radians(d)) for d, w in loc["directions"])
            directed_count = sum(w for _, w in loc["directions"])
            avg_dir = round(math.degrees(math.atan2(sin_sum, cos_sum)) % 360, 1)

        top_species = sorted(loc["species"].items(), key=lambda x: x[1], reverse=True)[:5]

        features.append({
            "lat": loc["lat"],
            "lng": loc["lng"],
            "loknavn": loc["loknavn"],
            "total": loc["total"],
            "direction": avg_dir,
            "directed_count": directed_count,
            "species": [{"name": n, "count": c} for n, c in top_species],
        })

    return features


@router.get("/api/map/heatmap")
async def api_map_heatmap(
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=30, ge=1, le=365),
    euring: str | None = Query(default=None),
    only_migrating: bool = Query(default=True),
):
    """Heatmap-data (vægtet pr. individer): [[lat, lng, intensity], ...].
    Uden euring returneres data for alle arter.
    """
    since = datetime.date.today() - datetime.timedelta(days=days)

    q = (
        select(
            Observation.lok_laengdegrad,
            Observation.lok_breddegrad,
            func.sum(Observation.antal).label("intensity"),
        )
        .where(
            Observation.dato >= since,
            Observation.lok_laengdegrad.isnot(None),
            Observation.lok_laengdegrad != "",
            Observation.antal.isnot(None),
            Observation.antal > 0,
        )
        .group_by(Observation.lok_laengdegrad, Observation.lok_breddegrad)
    )
    if only_migrating:
        q = q.where(Observation.adfkode == "T")
    if euring:
        q = q.where(Observation.artnr == euring)

    result = await db.execute(q)

    points = []
    for r in result.all():
        lat = _parse_coord(r.lok_breddegrad)
        lng = _parse_coord(r.lok_laengdegrad)
        if lat is not None and lng is not None:
            points.append([lat, lng, float(r.intensity)])

    return points


@router.get("/api/map/wind")
async def api_map_wind():
    """Hent aktuelle vinddata fra DMI stationer (seneste 2 timer)."""
    import httpx

    now = datetime.datetime.now(datetime.timezone.utc)
    dt_from = (now - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00Z")
    dt_to = now.strftime("%Y-%m-%dT%H:00:00Z")
    bbox = "7,54,16,58"  # Danmark
    base = f"{DMI_CLIMATE_BASE}/collections/stationValue/items"

    async with httpx.AsyncClient(timeout=30) as client:
        # Hent vindretning og vindhastighed parallelt
        speed_resp, dir_resp, gust_resp = await asyncio.gather(
            client.get(base, params={
                "parameterId": "mean_wind_speed",
                "timeResolution": "hour",
                "datetime": f"{dt_from}/{dt_to}",
                "bbox": bbox,
                "limit": "300",
                "sortorder": "from,DESC",
            }),
            client.get(base, params={
                "parameterId": "mean_wind_dir",
                "timeResolution": "hour",
                "datetime": f"{dt_from}/{dt_to}",
                "bbox": bbox,
                "limit": "300",
                "sortorder": "from,DESC",
            }),
            client.get(base, params={
                "parameterId": "max_wind_speed_10min",
                "timeResolution": "hour",
                "datetime": f"{dt_from}/{dt_to}",
                "bbox": bbox,
                "limit": "300",
                "sortorder": "from,DESC",
            }),
        )

    # Parse: grupper pr. station, tag seneste måling
    stations: dict[str, dict] = {}

    for resp, key in [
        (speed_resp, "speed"),
        (dir_resp, "direction"),
        (gust_resp, "gust"),
    ]:
        if resp.status_code != 200:
            continue
        data = resp.json()
        for f in data.get("features", []):
            props = f.get("properties", {})
            sid = props.get("stationId")
            if not sid:
                continue
            coords = f.get("geometry", {}).get("coordinates", [])
            if len(coords) < 2:
                continue
            if sid not in stations:
                stations[sid] = {
                    "stationId": sid,
                    "lng": coords[0],
                    "lat": coords[1],
                    "speed": None,
                    "direction": None,
                    "gust": None,
                    "time": props.get("from", ""),
                }
            # Tag kun seneste (sorteret DESC)
            if stations[sid][key] is None:
                stations[sid][key] = props.get("value")

    # Returner kun stationer med mindst hastighed
    result = [
        s for s in stations.values()
        if s["speed"] is not None
    ]

    return result


# ---------------------------------------------------------------------------
# Sync trigger
# ---------------------------------------------------------------------------

@router.post("/sync/{sync_type}")
async def trigger_sync(sync_type: str, background_tasks: BackgroundTasks):
    """Manuel synkronisering: species, phenology, observations, year."""
    if sync_type not in ("species", "phenology", "observations", "year", "historical", "migration_phenology"):
        return JSONResponse({"error": "invalid"}, status_code=400)

    async def _run(st: str):
        async with async_session() as session:
            if st == "species":
                await fetch_and_store_species(session)
            elif st == "phenology":
                await fetch_all_phenology(session)
            elif st == "observations":
                await fetch_and_store_observations(session, days=7)
            elif st == "year":
                await fetch_year_observations(session)
            elif st == "historical":
                await fetch_historical_observations(session, years=5)
            elif st == "migration_phenology":
                await build_migration_phenology(session)

    background_tasks.add_task(_run, sync_type)
    return {"status": "started", "type": sync_type}
