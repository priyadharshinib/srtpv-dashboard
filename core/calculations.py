"""All report calculations. Pure pandas: no Streamlit, no database.

Inputs are the DataFrames produced by core.importer (or loaded from the DB):
  inv: date, day, PV1..PV9, Total, temp_c, weather, wind_ms
  lt:  period, from_date, to_date, opening, closing, units, kwh, note
  ht:  date, day, import_kwh, export_kwh, net_import_kwh, note
"""
from __future__ import annotations

import pandas as pd

from core.config import (
    CELL_FLAG_FRACTION,
    INVERTER_IDS,
    INVERTERS,
    OUTAGE_FRACTION,
)


def headline_kpis(
    inv: pd.DataFrame,
    lt: pd.DataFrame,
    ht: pd.DataFrame,
    compare_through: pd.Timestamp | None = None,
) -> dict[str, float]:
    """Month-to-date totals.

    compare_through: last date used for the LT-vs-inverter comparison
    (exclude a day whose inverter data is incomplete). Defaults to all data.
    """
    inv_kwh = float(inv["Total"].sum())
    lt_kwh = float(lt["kwh"].sum())
    imp = float(ht["import_kwh"].sum())
    exp = float(ht["export_kwh"].sum())
    net = float(ht["net_import_kwh"].sum())
    site_load = net + lt_kwh

    if compare_through is None:
        compare_through = inv["date"].max()
    inv_cmp = float(inv.loc[inv["date"] <= compare_through, "Total"].sum())
    lt_cmp = float(lt.loc[lt["to_date"] <= compare_through, "kwh"].sum())

    return {
        "inverter_kwh": inv_kwh,
        "lt_kwh": lt_kwh,
        "ht_import_kwh": imp,
        "ht_export_kwh": exp,
        "ht_net_kwh": net,
        "site_load_kwh": site_load,
        "solar_share": lt_kwh / site_load if site_load else 0.0,
        "lt_vs_inverter": lt_cmp / inv_cmp - 1 if inv_cmp else 0.0,
        "compare_through": compare_through,
    }


def inverter_totals(inv: pd.DataFrame) -> pd.DataFrame:
    """Month-to-date kWh per inverter, and each against the average of the others."""
    kwh = inv[INVERTER_IDS].sum()
    total = kwh.sum()
    n_others = len(INVERTER_IDS) - 1
    out = pd.DataFrame({
        "inverter": INVERTER_IDS,
        "location": [INVERTERS[p] for p in INVERTER_IDS],
        "kwh": kwh.values,
    })
    out["vs_others"] = out["kwh"] / ((total - out["kwh"]) / n_others)
    out["zero_days"] = [
        inv.loc[inv[p] == 0, "date"].dt.strftime("%d %b").tolist() for p in INVERTER_IDS
    ]
    return out


def outage_mask(inv: pd.DataFrame) -> pd.Series:
    """True for days whose total is below OUTAGE_FRACTION of the month median.
    If the frame already carries an is_outage column (see filters.enrich), use it."""
    if "is_outage" in inv.columns:
        return inv["is_outage"].astype(bool)
    return inv["Total"] < inv["Total"].median() * OUTAGE_FRACTION


def flagged_cells(inv: pd.DataFrame) -> pd.DataFrame:
    """Boolean frame (same shape as PV columns): inverter < 50% of that day's median."""
    day_median = inv[INVERTER_IDS].median(axis=1)
    return inv[INVERTER_IDS].lt(day_median * CELL_FLAG_FRACTION, axis=0)


def daily_statement(inv: pd.DataFrame, lt: pd.DataFrame, ht: pd.DataFrame) -> pd.DataFrame:
    """One row per day: weather, inverter total, LT reading (single-day periods only), HT."""
    rows = []
    ht_by_date = ht.set_index("date")
    for _, r in inv.iterrows():
        d = r["date"]
        period = lt[(lt["from_date"] <= d) & (lt["to_date"] >= d)]
        lt_row = period.iloc[0] if not period.empty else None
        h = ht_by_date.loc[d] if d in ht_by_date.index else None
        rows.append({
            "date": d,
            "day": r["day"],
            "weather": r["weather"],
            "temp_c": r["temp_c"],
            "wind_ms": r["wind_ms"],
            "inverter_kwh": r["Total"],
            # LT kWh is shown only on the first day of its period
            "lt_kwh": lt_row["kwh"] if lt_row is not None and lt_row["from_date"] == d else None,
            "lt_period": lt_row["period"] if lt_row is not None else "Pending",
            "ht_import_kwh": h["import_kwh"] if h is not None else None,
            "ht_export_kwh": h["export_kwh"] if h is not None else None,
            "ht_net_kwh": h["net_import_kwh"] if h is not None else None,
        })
    return pd.DataFrame(rows)


def single_day_lt(lt: pd.DataFrame) -> pd.DataFrame:
    """LT periods that cover exactly one day (used for the dots on the daily chart)."""
    return lt[lt["from_date"] == lt["to_date"]][["from_date", "kwh"]].rename(columns={"from_date": "date"})
