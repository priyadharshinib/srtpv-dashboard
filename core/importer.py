"""Read and validate the three monthly input sheets.

Each import_* function returns an ImportResult. If `errors` is not empty,
the data must not be saved. `warnings` are shown to the user but allowed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from core.config import (
    HT_METER_MF,
    INVERTER_IDS,
    LT_METER_MF,
    TOTAL_MISMATCH_KWH,
)


@dataclass
class ImportResult:
    df: pd.DataFrame
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------- helpers ----------

def read_table(file) -> pd.DataFrame:
    """Read a CSV or Excel file (path or Streamlit UploadedFile)."""
    name = str(getattr(file, "name", file)).lower()
    if name.endswith(".csv"):
        df = pd.read_csv(file)
    elif name.endswith((".xlsx", ".xlsm")):
        df = pd.read_excel(file, engine="openpyxl")
    else:
        raise ValueError(f"Unsupported file type: {name}. Use .csv or .xlsx")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _parse_dates(series: pd.Series) -> pd.Series:
    """Accept ISO dates (2026-10-01), Excel dates, or Indian style 01-10-2026."""
    iso = pd.to_datetime(series, format="ISO8601", errors="coerce")
    fallback = pd.to_datetime(series, dayfirst=True, errors="coerce")
    return iso.fillna(fallback).dt.normalize()


def _missing(df: pd.DataFrame, required: list[str]) -> list[str]:
    return [c for c in required if c not in df.columns]


def _to_numeric(df: pd.DataFrame, cols: list[str], errors: list[str]) -> None:
    for c in cols:
        converted = pd.to_numeric(df[c], errors="coerce")
        bad = df.loc[converted.isna() & df[c].notna(), c]
        if not bad.empty:
            errors.append(f"Column {c}: non-numeric values {bad.tolist()[:5]}")
        df[c] = converted


def _check_dates(df: pd.DataFrame, col: str, errors: list[str]) -> None:
    df[col] = _parse_dates(df[col])
    if df[col].isna().any():
        errors.append(f"Column {col}: {int(df[col].isna().sum())} row(s) with an unreadable date")


# ---------- inverter sheet ----------

def import_inverter(file) -> ImportResult:
    """Columns: date, PV1..PV9, optional Total, temp_c, weather, wind_ms, day."""
    df = read_table(file)
    res = ImportResult(df)
    missing = _missing(df, ["date", *INVERTER_IDS])
    if missing:
        res.errors.append(f"Inverter sheet is missing columns: {missing}")
        return res

    df = df.dropna(subset=["date"]).copy()
    _check_dates(df, "date", res.errors)
    _to_numeric(df, INVERTER_IDS, res.errors)
    for opt in ("temp_c", "wind_ms"):
        if opt in df.columns:
            _to_numeric(df, [opt], res.errors)
        else:
            df[opt] = float("nan")
    if "weather" not in df.columns:
        df["weather"] = ""
    df["weather"] = df["weather"].fillna("").astype(str)

    dupes = df.loc[df["date"].duplicated(), "date"]
    if not dupes.empty:
        res.errors.append(f"Duplicate dates: {[d.date().isoformat() for d in dupes]}")

    neg = (df[INVERTER_IDS] < 0).any(axis=1)
    if neg.any():
        res.errors.append(f"Negative inverter kWh on {df.loc[neg, 'date'].dt.date.tolist()}")

    computed = df[INVERTER_IDS].sum(axis=1).round(2)
    if "Total" in df.columns:
        _to_numeric(df, ["Total"], res.errors)
        diff = (df["Total"] - computed).abs()
        bad = df.loc[diff > TOTAL_MISMATCH_KWH, "date"]
        if not bad.empty:
            res.warnings.append(
                f"Sheet Total differs from PV1–PV9 sum on {bad.dt.date.tolist()}; using the sum"
            )
    df["Total"] = computed
    df["day"] = df["date"].dt.strftime("%a")

    res.df = df.sort_values("date").reset_index(drop=True)[
        ["date", "day", *INVERTER_IDS, "Total", "temp_c", "weather", "wind_ms"]
    ]
    return res


# ---------- LT generation meter ----------

def import_lt_meter(file) -> ImportResult:
    """Columns: from_date, to_date, opening, closing; optional units, kwh, period, note."""
    df = read_table(file)
    res = ImportResult(df)
    missing = _missing(df, ["from_date", "to_date", "opening", "closing"])
    if missing:
        res.errors.append(f"LT meter sheet is missing columns: {missing}")
        return res

    df = df.dropna(subset=["from_date"]).copy()
    _check_dates(df, "from_date", res.errors)
    _check_dates(df, "to_date", res.errors)
    _to_numeric(df, ["opening", "closing"], res.errors)
    if res.errors:
        return res

    df = df.sort_values("from_date").reset_index(drop=True)
    df["units"] = (df["closing"] - df["opening"]).round(3)
    expected_kwh = (df["units"] * LT_METER_MF).round(1)

    if "kwh" in df.columns:
        _to_numeric(df, ["kwh"], res.errors)
        diff = (df["kwh"] - expected_kwh).abs()
        bad = df.loc[diff > 1, "from_date"]
        if not bad.empty:
            res.warnings.append(
                f"kWh ≠ units × {LT_METER_MF} for periods starting {bad.dt.date.tolist()}; using units × MF"
            )
    df["kwh"] = expected_kwh

    if (df["units"] < 0).any():
        res.errors.append("Closing reading lower than opening reading in at least one period")
    if (df["to_date"] < df["from_date"]).any():
        res.errors.append("A period ends before it starts")
    if (df["from_date"].iloc[1:].values <= df["to_date"].iloc[:-1].values).any():
        res.errors.append("LT periods overlap")
    jumps = df["opening"].iloc[1:].values != df["closing"].iloc[:-1].values
    if jumps.any():
        res.warnings.append("Opening reading does not match the previous closing in some periods")

    if "period" not in df.columns:
        df["period"] = None
    single = df["from_date"].dt.strftime("%d %b")
    combined = df["from_date"].dt.strftime("%d") + "–" + df["to_date"].dt.strftime("%d %b")
    auto = single.where(df["from_date"] == df["to_date"], combined)
    df["period"] = df["period"].fillna(auto)
    if "note" not in df.columns:
        df["note"] = ""
    df["note"] = df["note"].fillna("").astype(str)

    res.df = df[["period", "from_date", "to_date", "opening", "closing", "units", "kwh", "note"]]
    return res


# ---------- HT bi-directional meter ----------

def import_ht_meter(file) -> ImportResult:
    """Columns: date, import_kwh, export_kwh; optional net_import_kwh, note.

    If your sheet has meter display values instead, add columns
    import_units / export_units and they are converted with HT_METER_MF.
    """
    df = read_table(file)
    res = ImportResult(df)

    for kind in ("import", "export"):
        if f"{kind}_kwh" not in df.columns and f"{kind}_units" in df.columns:
            df[f"{kind}_kwh"] = pd.to_numeric(df[f"{kind}_units"], errors="coerce") * HT_METER_MF

    missing = _missing(df, ["date", "import_kwh", "export_kwh"])
    if missing:
        res.errors.append(f"HT meter sheet is missing columns: {missing}")
        return res

    df = df.dropna(subset=["date"]).copy()
    _check_dates(df, "date", res.errors)
    _to_numeric(df, ["import_kwh", "export_kwh"], res.errors)
    if ((df["import_kwh"] < 0) | (df["export_kwh"] < 0)).any():
        res.errors.append("Negative import/export values")
    dupes = df.loc[df["date"].duplicated(), "date"]
    if not dupes.empty:
        res.errors.append(f"Duplicate dates: {[d.date().isoformat() for d in dupes]}")

    df["net_import_kwh"] = df["import_kwh"] - df["export_kwh"]
    df["day"] = df["date"].dt.strftime("%a")
    if "note" not in df.columns:
        df["note"] = ""
    df["note"] = df["note"].fillna("").astype(str)

    res.df = df.sort_values("date").reset_index(drop=True)[
        ["date", "day", "import_kwh", "export_kwh", "net_import_kwh", "note"]
    ]
    return res


def load_sample() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """October 2026 sample data, used by tests and the 'Load sample' button."""
    folder = Path(__file__).resolve().parent.parent / "data" / "sample"
    inv = import_inverter(folder / "inverter_october_2026.csv").df
    lt = import_lt_meter(folder / "lt_meter_october_2026.csv").df
    ht = import_ht_meter(folder / "ht_meter_october_2026.csv").df
    return inv, lt, ht
