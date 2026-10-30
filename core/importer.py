"""Read and validate the three monthly input sheets.

Each import_* function returns an ImportResult. If `errors` is not empty,
the data must not be saved. `warnings` are shown to the user but allowed.
"""
from __future__ import annotations

import warnings
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
    with warnings.catch_warnings():           # ISO dates already parsed above; this only handles 01-10-2026 style
        warnings.simplefilter("ignore", UserWarning)
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


def _check_blanks(df: pd.DataFrame, cols: list[str], date_col: str, errors: list[str]) -> None:
    """Required numbers must not be empty (pandas also reads 'n/a', 'NA', '-' style text as empty)."""
    for c in cols:
        blank = df[c].isna()
        if blank.any():
            when = [d.strftime("%d %b") if pd.notna(d) else "?" for d in df.loc[blank, date_col]]
            errors.append(f"Column {c}: blank or non-numeric value on {when}")


def _check_dates(df: pd.DataFrame, col: str, errors: list[str]) -> None:
    df[col] = _parse_dates(df[col])
    if df[col].isna().any():
        errors.append(f"Column {col}: {int(df[col].isna().sum())} row(s) with an unreadable date")


# ---------- inverter sheet ----------

def _num(series: pd.Series) -> pd.Series:
    """'24.5°C' -> 24.5, '5.5 m/s' -> 5.5."""
    return pd.to_numeric(series.astype(str).str.extract(r"(-?\d+(?:\.\d+)?)")[0], errors="coerce")


def long_to_wide(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Portal export with one row per inverter per day (Date, Roof, Temperature, Weather,
    Wind Speed, Production, ..., Daily Total) -> one row per day with PV1..PV9."""
    errors: list[str] = []
    d = df.copy()
    d.columns = [str(c).strip() for c in d.columns]
    col = lambda *names: next((c for c in d.columns if c.lower().replace(" ", "").startswith(names)), None)
    c_date, c_roof, c_prod = col("date"), col("roof"), col("production")
    c_temp, c_wx, c_wind, c_total = col("temp"), col("weather"), col("wind"), col("dailytotal")
    raw = d[c_date].astype(str).str.strip()
    d["date"] = pd.to_datetime(raw, format="%d.%m.%Y", errors="coerce")
    rest = d["date"].isna() & d[c_date].notna()
    d.loc[rest, "date"] = _parse_dates(d.loc[rest, c_date])
    d = d[d["date"].notna() & d[c_roof].notna()].copy()        # drops the units row and the G.Total row
    d["inv"] = d[c_roof].astype(str).str.upper().str.extract(r"(PV\s*\d+)")[0].str.replace(" ", "", regex=False)
    bad = sorted(set(d.loc[~d["inv"].isin(INVERTER_IDS), c_roof].astype(str)))
    if bad:
        errors.append(f"Roof names without a known inverter (PV1–PV9): {bad[:5]}")
    d = d[d["inv"].isin(INVERTER_IDS)]
    fixes = []
    for day, rows in d.groupby("date", sort=False):
        invs = rows["inv"].tolist()
        dups = {p for p in invs if invs.count(p) > 1}
        if not dups:
            continue
        missing_inv = [p for p in INVERTER_IDS if p not in invs]
        # Unambiguous mislabel: one inverter twice, one missing, and the extra row sits
        # exactly where the missing inverter belongs in the PV1..PV9 order.
        if len(dups) == 1 and len(missing_inv) == 1 and len(invs) == len(INVERTER_IDS):
            pos = INVERTER_IDS.index(missing_inv[0])
            idx = rows.index[pos]
            if d.at[idx, "inv"] in dups:
                d.at[idx, "inv"] = missing_inv[0]
                fixes.append(f"{day:%d %b}: second '{list(dups)[0]}' row read as {missing_inv[0]} "
                             f"({d.at[idx, c_roof]} → {missing_inv[0]}, {d.at[idx, c_prod]} kWh)")
                continue
        vals = "; ".join(f"{r[c_roof]} = {r[c_prod]}" for _, r in rows[rows["inv"].isin(dups)].iterrows())
        errors.append(f"{day:%d %b}: the same inverter appears twice ({vals}). Correct the Roof name and import again.")
    if fixes:
        d.attrs["fixes"] = fixes
    d["prod"] = pd.to_numeric(d[c_prod], errors="coerce")
    wide = d.pivot_table(index="date", columns="inv", values="prod", aggfunc="first").reindex(columns=INVERTER_IDS)
    meta = d.groupby("date").agg(
        temp_c=(c_temp, lambda s: _num(s).dropna().iloc[0] if _num(s).notna().any() else float("nan")) if c_temp else ("date", "size"),
        weather=(c_wx, lambda s: next((str(x).strip() for x in s if pd.notna(x) and str(x).strip()), "")) if c_wx else ("date", "size"),
        wind_ms=(c_wind, lambda s: _num(s).dropna().iloc[0] if _num(s).notna().any() else float("nan")) if c_wind else ("date", "size"),
    )
    out = wide.join(meta).reset_index()
    out.attrs["fixes"] = d.attrs.get("fixes", [])
    if c_total:
        tot = pd.to_numeric(d[c_total], errors="coerce")
        out["Total"] = out["date"].map(d.assign(t=tot).dropna(subset=["t"]).groupby("date")["t"].last())
    for c in ("temp_c", "wind_ms"):
        if not c_temp and c == "temp_c" or not c_wind and c == "wind_ms":
            out[c] = float("nan")
    if not c_wx:
        out["weather"] = ""
    return out, errors



def import_inverter(file) -> ImportResult:
    """Columns: date, PV1..PV9, optional Total, temp_c, weather, wind_ms, day.
    Also accepts the portal's long export (one row per inverter per day: Date, Roof, Production...).

    Blank readings stay blank (Actuals); the Projected view fills them in the dashboard.
    A day with no inverter readings at all is left out.
    """
    df = read_table(file)
    res = ImportResult(df)
    cols = {str(c).strip().lower() for c in df.columns}
    if "roof" in cols and "production" in cols:
        df, long_errors = long_to_wide(df)
        if long_errors:
            res.errors.extend(long_errors)
            return res
        for fix in df.attrs.get("fixes", []):
            res.warnings.append(f"Corrected a duplicated inverter label — {fix}")
    missing = _missing(df, ["date", *INVERTER_IDS])
    if missing:
        res.errors.append(f"Inverter sheet is missing columns: {missing}")
        return res

    df = df.dropna(subset=["date"]).copy()
    _check_dates(df, "date", res.errors)
    _to_numeric(df, INVERTER_IDS, res.errors)          # text such as "abc" is still an error
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
    if res.errors:
        return res

    if "Total" in df.columns:
        _to_numeric(df, ["Total"], res.errors)
        complete = df[INVERTER_IDS].notna().all(axis=1)
        diff = (df["Total"] - df[INVERTER_IDS].sum(axis=1)).abs()
        bad = df.loc[complete & df["Total"].notna() & (diff > TOTAL_MISMATCH_KWH), "date"]
        if not bad.empty:
            res.warnings.append(f"Sheet Total differs from PV1–PV9 sum on {bad.dt.date.tolist()}; using the sum")

    df = df.sort_values("date").reset_index(drop=True)
    no_data = df[INVERTER_IDS].isna().all(axis=1)
    if no_data.any():
        res.warnings.append(f"No inverter readings on {', '.join(f'{d:%d %b}' for d in df.loc[no_data, 'date'])}: "
                            "left out")
        df = df[~no_data].reset_index(drop=True)
    blanks = [(f"{d:%d %b}", p) for d, row in zip(df["date"], df[INVERTER_IDS].isna().values)
              for p, b in zip(INVERTER_IDS, row) if b]
    if blanks:
        shown = "; ".join(f"{d} {p}" for d, p in blanks[:12]) + (" …" if len(blanks) > 12 else "")
        res.warnings.append(f"{len(blanks)} blank reading(s) kept blank in Actuals ({shown}); "
                            "the Projected view fills them where possible")
    df["Total"] = df[INVERTER_IDS].sum(axis=1, min_count=1).round(2)
    df["day"] = df["date"].dt.strftime("%a")
    for p in INVERTER_IDS:
        df[f"est_{p}"] = False
    res.df = df[["date", "day", *INVERTER_IDS, "Total", "temp_c", "weather", "wind_ms",
                 *[f"est_{p}" for p in INVERTER_IDS]]]
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
    _check_blanks(df, ["opening", "closing"], "from_date", res.errors)
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
    """Columns: date, import_kwh, export_kwh; optional net_import_kwh, power_factor, note.

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
    _check_blanks(df, ["import_kwh", "export_kwh"], "date", res.errors)
    if ((df["import_kwh"] < 0) | (df["export_kwh"] < 0)).any():
        res.errors.append("Negative import/export values")
    dupes = df.loc[df["date"].duplicated(), "date"]
    if not dupes.empty:
        res.errors.append(f"Duplicate dates: {[d.date().isoformat() for d in dupes]}")

    df["net_import_kwh"] = df["import_kwh"] - df["export_kwh"]
    for alias in ("pf", "PF", "Power Factor", "power factor"):
        if "power_factor" not in df.columns and alias in df.columns:
            df = df.rename(columns={alias: "power_factor"})
    if "power_factor" in df.columns:
        _to_numeric(df, ["power_factor"], res.errors)
        bad = df["power_factor"].notna() & ((df["power_factor"].abs() > 1) | (df["power_factor"] == 0))
        if bad.any():
            res.errors.append(f"power_factor must be between 0 and 1; check {df.loc[bad, 'date'].dt.date.tolist()}")
        df["power_factor"] = df["power_factor"].abs()
    else:
        df["power_factor"] = float("nan")
    df["day"] = df["date"].dt.strftime("%a")
    if "note" not in df.columns:
        df["note"] = ""
    df["note"] = df["note"].fillna("").astype(str)

    res.df = df.sort_values("date").reset_index(drop=True)[
        ["date", "day", "import_kwh", "export_kwh", "net_import_kwh", "power_factor", "note"]
    ]
    return res


def load_sample() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """October 2026 sample data, used by tests and the 'Load sample' button."""
    folder = Path(__file__).resolve().parent.parent / "data" / "sample"
    inv = import_inverter(folder / "inverter_october_2026.csv").df
    lt = import_lt_meter(folder / "lt_meter_october_2026.csv").df
    ht = import_ht_meter(folder / "ht_meter_october_2026.csv").df
    return inv, lt, ht
