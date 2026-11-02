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
from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, create_engine, delete, select, text, update
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
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)


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
    power_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class MonthlyBill(Base):
    """Figures from the monthly HT (BESCOM) bill. One row per month, e.g. '2026-09'."""
    __tablename__ = "monthly_bills"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    net_payable: Mapped[float | None] = mapped_column(Float, nullable=True)
    pf_penalty: Mapped[float | None] = mapped_column(Float, nullable=True)
    adjustment_label: Mapped[str] = mapped_column(String(100), default="")
    adjustment_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_pf: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_demand_kva: Mapped[float | None] = mapped_column(Float, nullable=True)
    md_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    contract_demand_kva: Mapped[float | None] = mapped_column(Float, nullable=True)
    export_note: Mapped[str] = mapped_column(Text, default="")
    lt_units: Mapped[float | None] = mapped_column(Float, nullable=True)
    lt_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    ht_import_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    ht_export_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    meter_note: Mapped[str] = mapped_column(Text, default="")
    inverter_note_title: Mapped[str] = mapped_column(Text, default="")
    inverter_note_body: Mapped[str] = mapped_column(Text, default="")


class InverterNote(Base):
    """Note shown under the inverter-wise table for a month. Written by admins only."""
    __tablename__ = "inverter_notes"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")


class MonthNote(Base):
    """Free-text engineering notes per month, e.g. '2026-10'."""
    __tablename__ = "month_notes"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")


class AppUser(Base):
    """Who may use the app and with which role. The login itself lives in Supabase Auth."""
    __tablename__ = "app_users"
    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(20), default="customer")


class ImportLog(Base):
    """Every import attempt, from the app or the email bot."""
    __tablename__ = "import_log"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(20))          # "app" or "email-bot"
    username: Mapped[str] = mapped_column(String(255))
    file: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20))            # Inverter / LT meter / HT meter / unknown
    status: Mapped[str] = mapped_column(String(20))          # saved / refused / skipped
    rows: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")


def init_db() -> None:
    if IS_POSTGRES:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"'))
    else:
        (ROOT / "data").mkdir(exist_ok=True)
    Base.metadata.create_all(engine)
    _migrate()


NEW_COLUMNS = [  # (table, column, postgres type, sqlite type)
    ("ht_meter_daily", "power_factor", "double precision", "FLOAT"),
    ("inverter_readings", "estimated", "boolean DEFAULT false", "BOOLEAN DEFAULT 0"),
    ("monthly_bills", "lt_units", "double precision", "FLOAT"),
    ("monthly_bills", "lt_kwh", "double precision", "FLOAT"),
    ("monthly_bills", "ht_import_kwh", "double precision", "FLOAT"),
    ("monthly_bills", "ht_export_kwh", "double precision", "FLOAT"),
    ("monthly_bills", "meter_note", "text DEFAULT ''", "TEXT DEFAULT ''"),
    ("monthly_bills", "inverter_note_title", "text DEFAULT ''", "TEXT DEFAULT ''"),
    ("monthly_bills", "inverter_note_body", "text DEFAULT ''", "TEXT DEFAULT ''"),
]


def _migrate() -> None:
    """Add columns introduced after the first release to existing tables."""
    with engine.begin() as conn:
        for table, col, pg_type, lite_type in NEW_COLUMNS:
            if IS_POSTGRES:
                conn.execute(text(f'ALTER TABLE "{DB_SCHEMA}".{table} ADD COLUMN IF NOT EXISTS {col} {pg_type}'))
            else:
                cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))]
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {lite_type}"))


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
            s.add_all(InverterReading(date=d, inverter=p, kwh=float(r[p]),
                                      estimated=bool(r.get(f"est_{p}", False)))
                      for p in INVERTER_IDS if pd.notna(r[p]))          # blank readings are not stored
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
                               net_import_kwh=float(r["net_import_kwh"]),
                               power_factor=_nan_to_none(r.get("power_factor")), note=r["note"]))


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


def data_bounds() -> tuple[dt.date, dt.date] | None:
    with Session(engine) as s:
        first = s.scalar(select(InverterReading.date).order_by(InverterReading.date).limit(1))
        last = s.scalar(select(InverterReading.date).order_by(InverterReading.date.desc()).limit(1))
    return (first, last) if first else None


def load_month(month: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return load_range(*_month_range(month))


def load_range(start: dt.date, end: dt.date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    with Session(engine) as s:
        readings = s.execute(
            select(InverterReading.date, InverterReading.inverter, InverterReading.kwh, InverterReading.estimated)
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

    long = pd.DataFrame(readings, columns=["date", "inverter", "kwh", "estimated"])
    long["estimated"] = long["estimated"].fillna(False).astype(bool)
    inv = long.pivot(index="date", columns="inverter", values="kwh").reindex(columns=INVERTER_IDS).reset_index()
    est = (long.pivot(index="date", columns="inverter", values="estimated").reindex(columns=INVERTER_IDS)
           .fillna(False).astype(bool).add_prefix("est_").reset_index())
    inv = inv.merge(pd.DataFrame(weather, columns=["date", "temp_c", "weather", "wind_ms"]), on="date", how="left")
    inv["date"] = pd.to_datetime(inv["date"])
    inv["day"] = inv["date"].dt.strftime("%a")
    inv["Total"] = inv[INVERTER_IDS].sum(axis=1).round(2)
    inv["weather"] = inv["weather"].fillna("")
    est["date"] = pd.to_datetime(est["date"])          # same date type as inv before joining
    # readings filled at import by an earlier version are not actual readings: show them blank
    for p in INVERTER_IDS:
        col = f"est_{p}"
        if col in est.columns and p in inv.columns:
            flag = inv["date"].map(est.set_index("date")[col]).fillna(False).astype(bool)
            inv.loc[flag.values, p] = float("nan")
    inv["Total"] = inv[INVERTER_IDS].sum(axis=1, min_count=1).round(2)
    inv = inv.merge(est, on="date", how="left")
    for p in INVERTER_IDS:
        inv[f"est_{p}"] = inv[f"est_{p}"].fillna(False).astype(bool) if f"est_{p}" in inv else False
    inv = inv[["date", "day", *INVERTER_IDS, "Total", "temp_c", "weather", "wind_ms",
               *[f"est_{p}" for p in INVERTER_IDS]]].sort_values("date")

    lt = pd.DataFrame([{
        "period": r.period, "from_date": pd.Timestamp(r.from_date), "to_date": pd.Timestamp(r.to_date),
        "opening": r.opening, "closing": r.closing, "units": r.units, "kwh": r.kwh, "note": r.note,
    } for r in lt_rows], columns=["period", "from_date", "to_date", "opening", "closing", "units", "kwh", "note"])

    ht = pd.DataFrame([{
        "date": pd.Timestamp(r.date), "day": r.date.strftime("%a"), "import_kwh": r.import_kwh,
        "export_kwh": r.export_kwh, "net_import_kwh": r.net_import_kwh,
        "power_factor": r.power_factor, "note": r.note,
    } for r in ht_rows], columns=["date", "day", "import_kwh", "export_kwh", "net_import_kwh", "power_factor", "note"])

    return inv.reset_index(drop=True), lt, ht


def save_inverter_note(month: str, text: str) -> None:
    with Session(engine) as s, s.begin():
        s.merge(InverterNote(month=month, text=text))


def load_inverter_note(month: str) -> str:
    with Session(engine) as s:
        note = s.get(InverterNote, month)
    return note.text if note else ""


def load_note(month: str) -> str:
    with Session(engine) as s:
        note = s.get(MonthNote, month)
    return note.text if note else ""


# ---------- user access ----------

def get_user_role(email: str) -> str | None:
    with Session(engine) as s:
        u = s.get(AppUser, email.lower())
    return u.role if u else None


def list_users() -> pd.DataFrame:
    with Session(engine) as s:
        rows = s.scalars(select(AppUser).order_by(AppUser.role, AppUser.email)).all()
    return pd.DataFrame([{"email": u.email, "name": u.name, "role": u.role} for u in rows],
                        columns=["email", "name", "role"])


def save_user(email: str, name: str, role: str) -> None:
    with Session(engine) as s, s.begin():
        s.merge(AppUser(email=email.strip().lower(), name=name.strip(), role=role))


def remove_user(email: str) -> None:
    with Session(engine) as s, s.begin():
        s.execute(delete(AppUser).where(AppUser.email == email.lower()))


# ---------- import log ----------

def log_import(source: str, user: str, file: str, kind: str, status: str, rows: int = 0, message: str = "") -> None:
    with Session(engine) as s, s.begin():
        s.add(ImportLog(at=dt.datetime.now(dt.timezone.utc), source=source, username=user, file=file[:255],
                        kind=kind, status=status, rows=rows, message=message[:2000]))


def recent_imports(limit: int = 30) -> pd.DataFrame:
    with Session(engine) as s:
        rows = s.scalars(select(ImportLog).order_by(ImportLog.at.desc()).limit(limit)).all()
    return pd.DataFrame([{
        "when (IST)": pd.Timestamp(r.at).tz_convert("Asia/Kolkata").strftime("%d %b %Y %H:%M") if r.at.tzinfo
                      else pd.Timestamp(r.at).strftime("%d %b %Y %H:%M"),
        "source": r.source, "user": r.username, "file": r.file, "kind": r.kind,
        "status": r.status, "rows": r.rows, "message": r.message,
    } for r in rows], columns=["when (IST)", "source", "user", "file", "kind", "status", "rows", "message"])


# ---------- automatic weather ----------

def dates_missing_weather(since: dt.date) -> list[dt.date]:
    """Days that have inverter readings but no weather text."""
    with Session(engine) as s:
        days = set(s.scalars(select(InverterReading.date).where(InverterReading.date >= since).distinct()).all())
        have = set(s.scalars(select(DailyWeather.date).where(
            DailyWeather.date >= since, DailyWeather.weather.is_not(None), DailyWeather.weather != "")).all())
    return sorted(days - have)


def fill_weather(rows: pd.DataFrame) -> int:
    """rows: date, temp_c, weather, wind_ms. Only fills days whose weather is still empty."""
    n = 0
    with Session(engine) as s, s.begin():
        for r in rows.itertuples():
            d = r.date.date() if hasattr(r.date, "date") else r.date
            w = s.get(DailyWeather, d)
            if w is None:
                s.add(DailyWeather(date=d, temp_c=_nan_to_none(r.temp_c), weather=r.weather,
                                   wind_ms=_nan_to_none(r.wind_ms)))
                n += 1
            elif not w.weather:
                w.weather = r.weather
                w.temp_c = w.temp_c if w.temp_c is not None else _nan_to_none(r.temp_c)
                w.wind_ms = w.wind_ms if w.wind_ms is not None else _nan_to_none(r.wind_ms)
                n += 1
    return n


# ---------- monthly HT bill ----------

BILL_FIELDS = ["net_payable", "pf_penalty", "adjustment_label", "adjustment_amount", "avg_pf",
               "max_demand_kva", "md_at", "contract_demand_kva", "export_note",
               "lt_units", "lt_kwh", "ht_import_kwh", "ht_export_kwh",
               "meter_note", "inverter_note_title", "inverter_note_body"]


def save_bill(month: str, values: dict) -> None:
    with Session(engine) as s, s.begin():
        s.merge(MonthlyBill(month=month, **{k: values.get(k) for k in BILL_FIELDS}))


def load_bill(month: str) -> dict | None:
    with Session(engine) as s:
        b = s.get(MonthlyBill, month)
    return {k: getattr(b, k) for k in BILL_FIELDS} if b else None


# ---------- month coverage (for importing a month in parts) ----------

def coverage(month: str) -> dict[str, list[dt.date]]:
    """Days already saved in a month, per sheet type."""
    start, end = _month_range(month)
    with Session(engine) as s:
        inv_days = s.scalars(select(InverterReading.date).where(InverterReading.date.between(start, end))
                             .distinct()).all()
        ht_days = s.scalars(select(HtMeterDaily.date).where(HtMeterDaily.date.between(start, end))).all()
        lt_periods = s.execute(select(LtMeterPeriod.from_date, LtMeterPeriod.to_date)
                               .where(LtMeterPeriod.from_date.between(start, end))).all()
    lt_days = {d.date() for a, b in lt_periods for d in pd.date_range(a, b)}
    return {"inverter": sorted(set(inv_days)), "lt": sorted(lt_days), "ht": sorted(set(ht_days))}


def lt_closing_before(day: dt.date) -> float | None:
    """Closing reading of the last LT period that ends before this day."""
    with Session(engine) as s:
        return s.scalar(select(LtMeterPeriod.closing).where(LtMeterPeriod.to_date < day)
                        .order_by(LtMeterPeriod.to_date.desc()).limit(1))



def clear_month(month: str, kinds: list[str]) -> dict[str, int]:
    """Delete a month's data before re-importing it. kinds: inverter, lt, ht, bill."""
    start, end = _month_range(month)
    done = {}
    with Session(engine) as s, s.begin():
        if "inverter" in kinds:
            done["inverter"] = s.execute(delete(InverterReading).where(InverterReading.date.between(start, end))).rowcount
            s.execute(delete(DailyWeather).where(DailyWeather.date.between(start, end)))
        if "lt" in kinds:
            done["lt"] = s.execute(delete(LtMeterPeriod).where(LtMeterPeriod.from_date.between(start, end))).rowcount
        if "ht" in kinds:
            done["ht"] = s.execute(delete(HtMeterDaily).where(HtMeterDaily.date.between(start, end))).rowcount
        if "bill" in kinds:
            done["bill"] = s.execute(delete(MonthlyBill).where(MonthlyBill.month == month)).rowcount
    return done


# ---------- projected view: share per month ----------

class ProjectionSetting(Base):
    """Share of each day's total given to a chronically faulty inverter in the Projected view."""
    __tablename__ = "projection_settings"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    share: Mapped[float] = mapped_column(Float)


def get_projection_shares() -> dict[str, float]:
    with Session(engine) as s:
        return {r.month: r.share for r in s.scalars(select(ProjectionSetting)).all()}


def save_projection_share(month: str, share: float) -> None:
    with Session(engine) as s, s.begin():
        s.merge(ProjectionSetting(month=month, share=float(share)))
