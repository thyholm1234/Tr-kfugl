from __future__ import annotations

import datetime
from sqlalchemy import (
    Boolean,
    String,
    Integer,
    Float,
    Date,
    DateTime,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import ARRAY

from app.database import Base


class Species(Base):
    """Arter fra DOFbasen artsliste."""

    __tablename__ = "species"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    euring: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    sortering: Mapped[int] = mapped_column(Integer)
    artnavn: Mapped[str] = mapped_column(String(200))
    latin: Mapped[str] = mapped_column(String(200))
    english: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10))  # A, SU, AU, etc.
    art_type: Mapped[str] = mapped_column(String(20))  # art, hybrid, underart

    def __repr__(self) -> str:
        return f"<Species {self.artnavn} ({self.euring})>"


class Phenology(Base):
    """Fænologidata i 10-dages perioder (36 pr. år) for en art."""

    __tablename__ = "phenology"
    __table_args__ = (
        UniqueConstraint("euring", "period_index", name="uq_phenology_euring_period_index"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    euring: Mapped[str] = mapped_column(String(10), index=True)
    period_index: Mapped[int] = mapped_column(Integer)  # 0-35
    avg_value: Mapped[float] = mapped_column(Float, default=0.0)
    last_year_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_year_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_first_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_last_year: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Observation(Base):
    """Observationer hentet fra DOFbasen."""

    __tablename__ = "observations"
    __table_args__ = (
        Index("ix_obs_artnr_dato", "artnr", "dato"),
        UniqueConstraint("obsid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dato: Mapped[datetime.date] = mapped_column(Date, index=True)
    turtidfra: Mapped[str | None] = mapped_column(String(10), nullable=True)
    turtidtil: Mapped[str | None] = mapped_column(String(10), nullable=True)
    loknr: Mapped[str | None] = mapped_column(String(20), nullable=True)
    loknavn: Mapped[str | None] = mapped_column(Text, nullable=True)
    artnr: Mapped[str] = mapped_column(String(10), index=True)
    artnavn: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latin: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sortering: Mapped[int | None] = mapped_column(Integer, nullable=True)
    antal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    koen: Mapped[str | None] = mapped_column(String(10), nullable=True)
    adfkode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    adfbeskrivelse: Mapped[str | None] = mapped_column(String(100), nullable=True)
    alderkode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dragtkode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dragtbeskrivelse: Mapped[str | None] = mapped_column(String(100), nullable=True)
    obserkode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fornavn: Mapped[str | None] = mapped_column(String(100), nullable=True)
    efternavn: Mapped[str | None] = mapped_column(String(100), nullable=True)
    obser_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    medobser: Mapped[str | None] = mapped_column(Text, nullable=True)
    turnoter: Mapped[str | None] = mapped_column(Text, nullable=True)
    fuglnoter: Mapped[str | None] = mapped_column(Text, nullable=True)
    metode: Mapped[str | None] = mapped_column(String(100), nullable=True)
    obstidfra: Mapped[str | None] = mapped_column(String(10), nullable=True)
    obstidtil: Mapped[str | None] = mapped_column(String(10), nullable=True)
    hemmelig: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kvalitet: Mapped[int | None] = mapped_column(Integer, nullable=True)
    turid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    obsid: Mapped[int] = mapped_column(Integer, unique=True)
    dof_afdeling: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lok_laengdegrad: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lok_breddegrad: Mapped[str | None] = mapped_column(String(50), nullable=True)
    obs_laengdegrad: Mapped[str | None] = mapped_column(String(50), nullable=True)
    obs_breddegrad: Mapped[str | None] = mapped_column(String(50), nullable=True)
    radius: Mapped[str | None] = mapped_column(String(20), nullable=True)
    obser_laengdegrad: Mapped[str | None] = mapped_column(String(50), nullable=True)
    obser_breddegrad: Mapped[str | None] = mapped_column(String(50), nullable=True)


class DataSync(Base):
    """Track hvornår data sidst blev synkroniseret."""

    __tablename__ = "data_sync"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sync_type: Mapped[str] = mapped_column(String(50), unique=True)
    last_sync: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class ScheduleConfig(Base):
    """Konfiguration af planlagte synkroniseringer."""

    __tablename__ = "schedule_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sync_type: Mapped[str] = mapped_column(String(50), unique=True)
    cron_expression: Mapped[str] = mapped_column(String(100), default="")
    enabled: Mapped[bool] = mapped_column(default=False)


class MigrationPhenology(Base):
    """Fænologi beregnet KUN fra observationer med trækadfærd (adfkode='T').

    Bygges fra historiske observationer (4-5 år) i 36 ti-dages perioder.
    Giver et reelt billede af hvornår fuglene trækker, uafhængigt af
    DOFbasens generelle fænologidata som inkluderer alle observationer.
    """

    __tablename__ = "migration_phenology"
    __table_args__ = (
        UniqueConstraint(
            "euring", "period_index",
            name="uq_migration_phen_euring_period",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    euring: Mapped[str] = mapped_column(String(10), index=True)
    period_index: Mapped[int] = mapped_column(Integer)  # 0-35
    avg_individuals: Mapped[float] = mapped_column(Float, default=0.0)
    avg_obs_count: Mapped[float] = mapped_column(Float, default=0.0)
    year_count: Mapped[int] = mapped_column(Integer, default=0)  # antal år med data
    last_rebuilt: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
