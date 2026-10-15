"""Database storage with SQLAlchemy 2.0.

Local default: SQLite at data/solar.db.
Online: set DATABASE_URL (env, .env or Streamlit secrets) to the Supabase
session-pooler string. Tables then live in their own schema ("solar"),
which Supabase's public Data API does not expose.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
from sqlalchemy import Date, Float, String, Text, create_engine, delete, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from core.config import INVERTER_IDS
from core.settings import get_setting

ROOT = Path(__file__).resolve().parent.parent


def _normalise(url: str) -> str:
    """Accept the string exactly as Supabase shows it and use the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DATABASE_URL = _normalise(get_setting("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'solar.db'}"))
IS_POSTGRES = DATABASE_URL.startswith("postgresql")
DB_SCHEMA = get_setting("DB_SCHEMA", "solar")

if IS_POSTGRES:
    engine = create_engine(
        DATABASE_URL,
        pool_size=3, max_overflow=2, pool_pre_ping=True, pool_recycle=300,
        # every table below is created and queried inside DB_SCHEMA
        execution_options={"schema_translate_map": {None: DB_SCHEMA}},
    )
else:
    engine = create_engine(DATABASE_URL)


class Base(DeclarativeBase):
    pass


class InverterReading(Base):
    """Long format: one row per inverter per day (scales to more inverters/plants)."""
    __tablename__ = "inverter_readings"
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    inverter: Mapped[str] = mapped_column(String(10), primary_key=True)
    kwh: Mapped[float] = mapped_column(Float)


class DailyWeather(Base):
    __tablename__ = "daily_weather"
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    weather: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wind_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


class LtMeterPeriod(Base):
    __tablename__ = "lt_meter_periods"
    from_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    to_date: Mapped[dt.date] = mapped_column(Date)
    period: Mapped[str] = mapped_column(String(30))
    opening: Mapped[float] = mapped_column(Float)
    closing: Mapped[float] = mapped_column(Float)
    units: Mapped[float] = mapped_column(Float)
    kwh: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")


class HtMeterDaily(Base):
    __tablename__ = "ht_meter_daily"
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    import_kwh: Mapped[float] = mapped_column(Float)
    export_kwh: Mapped[float] = mapped_column(Float)
    net_import_kwh: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")


class MonthNote(Base):
    """Free-text engineering notes per month, e.g. '2026-10'."""
    __tablename__ = "month_notes"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")


def init_db() -> None:
    if IS_POSTGRES:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"'))
    else:
        (ROOT / "data").mkdir(exist_ok=True)
    Base.metadata.create_all(engine)


def database_label() -> str:
    """Shown in the sidebar so you know which database the app is using."""
    return f"Supabase / PostgreSQL (schema {DB_SCHEMA})" if IS_POSTGRES else "Local SQLite"


def _nan_to_none(v):
    return None if pd.isna(v) else v


# ---------- save (re-importing a date replaces it) ----------

def save_inverter(inv: pd.DataFrame) -> None:
    dates = [d.date() for d in inv["date"]]
    with Session(engine) as s, s.begin():
        s.execute(delete(InverterReading).where(InverterReading.date.in_(dates)))
        s.execute(delete(DailyWeather).where(DailyWeather.date.in_(dates)))
        for _, r in inv.iterrows():
            d = r["date"].date()
            s.add_all(InverterReading(date=d, inverter=p, kwh=float(r[p])) for p in INVERTER_IDS)
            s.add(DailyWeather(date=d, temp_c=_nan_to_none(r["temp_c"]),
                               weather=r["weather"] or None, wind_ms=_nan_to_none(r["wind_ms"])))


def save_lt(lt: pd.DataFrame) -> None:
    starts = [d.date() for d in lt["from_date"]]
    with Session(engine) as s, s.begin():
        s.execute(delete(LtMeterPeriod).where(LtMeterPeriod.from_date.in_(starts)))
        for _, r in lt.iterrows():
            s.add(LtMeterPeriod(from_date=r["from_date"].date(), to_date=r["to_date"].date(),
                                period=r["period"], opening=float(r["opening"]), closing=float(r["closing"]),
                                units=float(r["units"]), kwh=float(r["kwh"]), note=r["note"]))


def save_ht(ht: pd.DataFrame) -> None:
    dates = [d.date() for d in ht["date"]]
    with Session(engine) as s, s.begin():
        s.execute(delete(HtMeterDaily).where(HtMeterDaily.date.in_(dates)))
        for _, r in ht.iterrows():
            s.add(HtMeterDaily(date=r["date"].date(), import_kwh=float(r["import_kwh"]),
                               export_kwh=float(r["export_kwh"]),
                               net_import_kwh=float(r["net_import_kwh"]), note=r["note"]))


def save_note(month: str, text: str) -> None:
    with Session(engine) as s, s.begin():
        s.merge(MonthNote(month=month, text=text))


# ---------- load one month, in the same shape the importer produces ----------

def _month_range(month: str) -> tuple[dt.date, dt.date]:
    start = dt.date.fromisoformat(month + "-01")
    end = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
    return start, end


def available_months() -> list[str]:
    with Session(engine) as s:
        dates = s.scalars(select(InverterReading.date).distinct()).all()
    return sorted({d.strftime("%Y-%m") for d in dates}, reverse=True)


def load_month(month: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    start, end = _month_range(month)
    with Session(engine) as s:
        readings = s.execute(
            select(InverterReading.date, InverterReading.inverter, InverterReading.kwh)
            .where(InverterReading.date.between(start, end))
        ).all()
        weather = s.execute(
            select(DailyWeather.date, DailyWeather.temp_c, DailyWeather.weather, DailyWeather.wind_ms)
            .where(DailyWeather.date.between(start, end))
        ).all()
        lt_rows = s.scalars(
            select(LtMeterPeriod).where(LtMeterPeriod.from_date.between(start, end))
            .order_by(LtMeterPeriod.from_date)
        ).all()
        ht_rows = s.scalars(
            select(HtMeterDaily).where(HtMeterDaily.date.between(start, end)).order_by(HtMeterDaily.date)
        ).all()

    long = pd.DataFrame(readings, columns=["date", "inverter", "kwh"])
    inv = long.pivot(index="date", columns="inverter", values="kwh").reindex(columns=INVERTER_IDS).reset_index()
    inv = inv.merge(pd.DataFrame(weather, columns=["date", "temp_c", "weather", "wind_ms"]), on="date", how="left")
    inv["date"] = pd.to_datetime(inv["date"])
    inv["day"] = inv["date"].dt.strftime("%a")
    inv["Total"] = inv[INVERTER_IDS].sum(axis=1).round(2)
    inv["weather"] = inv["weather"].fillna("")
    inv = inv[["date", "day", *INVERTER_IDS, "Total", "temp_c", "weather", "wind_ms"]].sort_values("date")

    lt = pd.DataFrame([{
        "period": r.period, "from_date": pd.Timestamp(r.from_date), "to_date": pd.Timestamp(r.to_date),
        "opening": r.opening, "closing": r.closing, "units": r.units, "kwh": r.kwh, "note": r.note,
    } for r in lt_rows], columns=["period", "from_date", "to_date", "opening", "closing", "units", "kwh", "note"])

    ht = pd.DataFrame([{
        "date": pd.Timestamp(r.date), "day": r.date.strftime("%a"), "import_kwh": r.import_kwh,
        "export_kwh": r.export_kwh, "net_import_kwh": r.net_import_kwh, "note": r.note,
    } for r in ht_rows], columns=["date", "day", "import_kwh", "export_kwh", "net_import_kwh", "note"])

    return inv.reset_index(drop=True), lt, ht


def load_note(month: str) -> str:
    with Session(engine) as s:
        note = s.get(MonthNote, month)
    return note.text if note else ""
