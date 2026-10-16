"""Derived columns (productivity, weather group) and report filters. Pure pandas."""
from __future__ import annotations

import pandas as pd

from core.calculations import outage_mask
from core.config import PLANT_CAPACITY_KWP, PRODUCTIVITY_BANDS


def weather_group(text: str) -> str:
    s = (text or "").lower()
    if not s.strip():
        return "Not recorded"
    if any(k in s for k in ("rain", "thunder", "storm", "shower", "drizzle")):
        return "Rain / storm"
    if "partly" in s:
        return "Partly cloudy"
    if any(k in s for k in ("sunny", "clear", "bright")):
        return "Sunny"
    if any(k in s for k in ("cloud", "overcast", "haze", "hazy", "mist", "fog")):
        return "Cloudy"
    return "Not recorded"


def productivity_band(yield_kwh_kwp: float) -> str:
    for name, low, high in PRODUCTIVITY_BANDS:
        if low <= yield_kwh_kwp < high:
            return name
    return PRODUCTIVITY_BANDS[-1][0]


def enrich(inv: pd.DataFrame) -> pd.DataFrame:
    """Add columns used by filters. Outage is judged on the whole loaded period,
    so filtering to e.g. rainy days doesn't change which days count as outages."""
    out = inv.copy()
    out["yield_kwh_kwp"] = out["Total"] / PLANT_CAPACITY_KWP
    out["productivity"] = out["yield_kwh_kwp"].map(productivity_band)
    out["weather_group"] = out["weather"].map(weather_group)
    out["day_type"] = out["date"].dt.dayofweek.map(lambda d: "Weekend" if d >= 5 else "Weekday")
    out["is_outage"] = outage_mask(inv).values
    return out


def apply_filters(
    inv: pd.DataFrame,
    lt: pd.DataFrame,
    ht: pd.DataFrame,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    weather: list[str] | None = None,
    productivity: list[str] | None = None,
    day_types: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """inv must come from enrich(). Returns filtered (inv, lt, ht).
    An LT period is kept only if every day it covers is kept."""
    keep = pd.Series(True, index=inv.index)
    if start is not None:
        keep &= inv["date"] >= start
    if end is not None:
        keep &= inv["date"] <= end
    if weather is not None:
        keep &= inv["weather_group"].isin(weather)
    if productivity is not None:
        keep &= inv["productivity"].isin(productivity)
    if day_types is not None:
        keep &= inv["day_type"].isin(day_types)

    f_inv = inv[keep].reset_index(drop=True)
    days = set(f_inv["date"])
    lt_keep = [
        all(d in days for d in pd.date_range(r.from_date, r.to_date))
        for r in lt.itertuples()
    ]
    f_lt = lt[lt_keep].reset_index(drop=True) if len(lt) else lt
    f_ht = ht[ht["date"].isin(days)].reset_index(drop=True)
    return f_inv, f_lt, f_ht


def productivity_summary(inv: pd.DataFrame) -> pd.DataFrame:
    """Days and kWh per productivity band (bands with no days included, in order)."""
    names = [b[0] for b in PRODUCTIVITY_BANDS]
    g = inv.groupby("productivity")["Total"].agg(days="count", kwh="sum").reindex(names, fill_value=0)
    ranges = {n: (f"{lo:g}–{hi:g}" if hi != float("inf") else f"≥ {lo:g}") for n, lo, hi in PRODUCTIVITY_BANDS}
    g.insert(0, "kWh/kWp/day", [ranges[n] for n in names])
    return g.reset_index(names="band")
