import os

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://fugl:fugl_secret@localhost:5432/traekfugl",
)

DATABASE_URL_SYNC: str = os.getenv(
    "DATABASE_URL_SYNC",
    "postgresql+psycopg2://fugl:fugl_secret@localhost:5432/traekfugl",
)

DOFBASEN_SPECIES_URL = (
    "https://dofbasen.dk/excel/search_result1.php?obstype=artsliste"
)

DOFBASEN_PHENOLOGY_URL = (
    "https://service.dofbasen.dk/DanmarksFugleBackend/api/art/phenology"
)

DOFBASEN_OBSERVATIONS_URL = (
    "https://dofbasen.dk/excel/search_result1.php"
    "?design=excel&soeg=soeg&periode=antaldage"
    "&obstype=observationer&species=alle&sortering=dato"
)

DOFBASEN_SPECIES_PAGE_URL = "https://dofbasen.dk/danmarksfugle/art/{artnr}"

# DMI Open Data
DMI_CLIMATE_BASE = "https://opendataapi.dmi.dk/v2/climateData"
