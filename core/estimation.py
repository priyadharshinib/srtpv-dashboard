"""Estimates, always marked as such in the dashboard.

1. fill_missing_inverters: a missing inverter reading on a day is filled so the day
   matches the sheet's Total (if given), otherwise from that inverter's usual share
   of the plant output. A day with no inverter data at all is left as "no data".

2. bill_daily: when the month's HT bill gives LT and HT totals, spread them over the
   days exactly as the monthly report does:
     - a no-data day gets LT total / days in month; the rest of LT follows inverter output
     - site load by day type (weekday / Sat / Sun weights), scaled to the billed total
     - HT import = site load - LT + export; export from the daily meter where available
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.config import INVERTER_IDS, SITE_LOAD_WEIGHTS


def inverter_shares(inv: pd.DataFrame) -> pd.Series:
    """Each inverter's typical share of the plant total, from complete days."""
    vals = inv[INVERTER_IDS]
    complete = vals.notna().all(axis=1) & (vals.sum(axis=1) > 0)
    if complete.any():
        part = vals[complete].div(vals[complete].sum(axis=1), axis=0).median()
        if part.sum() > 0:
            return part / part.sum()
    return pd.Series(1 / len(INVERTER_IDS), index=INVERTER_IDS)


def fill_missing_inverters(inv: pd.DataFrame, sheet_total: pd.Series | None = None):
    """Return (filled inv with est_PV1..est_PV9 flags, list of no-data dates, list of notes)."""
    inv = inv.copy().reset_index(drop=True)
    if sheet_total is not None:
        sheet_total = pd.Series(sheet_total).reset_index(drop=True)
    est = inv[INVERTER_IDS].isna()
    shares = inverter_shares(inv)
    nodata_rows, notes = [], []
    for i in inv.index:
        miss = [p for p in INVERTER_IDS if pd.isna(inv.at[i, p])]
        if not miss:
            continue
        present = [p for p in INVERTER_IDS if p not in miss]
        have = float(sum(inv.at[i, p] for p in present))
        total = sheet_total.iloc[i] if sheet_total is not None else np.nan
        if pd.notna(total) and total > have:
            remaining, how = float(total) - have, "to match the day's Total"
        elif present and shares[present].sum() > 0:
            remaining = have / shares[present].sum() * shares[miss].sum()
            how = "from each inverter's usual share"
        else:
            nodata_rows.append(i)
            continue
        w = shares[miss]
        w = w / w.sum() if w.sum() > 0 else pd.Series(1 / len(miss), index=miss)
        for p in miss:
            inv.at[i, p] = round(remaining * w[p], 2)
        notes.append(f"{inv.at[i, 'date']:%d %b}: estimated {', '.join(miss)} {how}")
    nodata = [inv.at[i, "date"] for i in nodata_rows]
    inv = inv.drop(index=nodata_rows)
    est = est.drop(index=nodata_rows)
    inv["Total"] = inv[INVERTER_IDS].sum(axis=1).round(2)
    for p in INVERTER_IDS:
        inv[f"est_{p}"] = est[p].values
    return inv.reset_index(drop=True), nodata, notes


def bill_has_totals(bill: dict | None) -> bool:
    return bool(bill) and all(bill.get(k) is not None for k in ("lt_kwh", "ht_import_kwh", "ht_export_kwh"))


def bill_daily(inv: pd.DataFrame, ht: pd.DataFrame, bill: dict, month: str) -> pd.DataFrame:
    """One row per calendar day of the month with estimated LT, HT and site load.
    Columns: date, inverter_kwh (actual or NaN), inverter_est (no-data days),
    lt_kwh, ht_import_kwh, ht_export_kwh, ht_net_kwh, site_load_kwh, power_factor, no_data."""
    start = pd.Timestamp(month + "-01")
    days = pd.date_range(start, start + pd.offsets.MonthEnd(0))
    d = pd.DataFrame({"date": days})
    d = d.merge(inv[["date", "Total"]].rename(columns={"Total": "inverter_kwh"}), on="date", how="left")
    d["no_data"] = d["inverter_kwh"].isna()

    lt_total = float(bill["lt_kwh"])
    lt_flat = lt_total / len(days)
    known = float(d["inverter_kwh"].sum())
    k = (lt_total - lt_flat * d["no_data"].sum()) / known if known else 0.0
    d["lt_kwh"] = np.where(d["no_data"], lt_flat, d["inverter_kwh"] * k)
    d["inverter_est"] = np.where(d["no_data"] & (k > 0), lt_flat / k if k else np.nan, np.nan)

    exp_total = float(bill["ht_export_kwh"])
    ht_by_date = ht.set_index("date") if len(ht) else pd.DataFrame(columns=["export_kwh", "power_factor"])
    exp = d["date"].map(ht_by_date["export_kwh"]).fillna(0.0) if len(ht) else pd.Series(0.0, index=d.index)
    if exp.sum() > 0:
        exp = exp * (exp_total / exp.sum())
    elif exp_total > 0:                       # no daily export readings: spread over weekends
        wk = d["date"].dt.dayofweek >= 5
        exp = pd.Series(np.where(wk, exp_total / max(wk.sum(), 1), 0.0), index=d.index)
    d["ht_export_kwh"] = exp.values

    site_total = float(bill["ht_import_kwh"]) - exp_total + lt_total
    dow = d["date"].dt.dayofweek
    w = np.where(dow == 5, SITE_LOAD_WEIGHTS["Sat"], np.where(dow == 6, SITE_LOAD_WEIGHTS["Sun"],
                                                              SITE_LOAD_WEIGHTS["weekday"]))
    d["site_load_kwh"] = site_total * w / w.sum()
    d["ht_import_kwh"] = d["site_load_kwh"] - d["lt_kwh"] + d["ht_export_kwh"]
    d["ht_net_kwh"] = d["ht_import_kwh"] - d["ht_export_kwh"]
    d["power_factor"] = d["date"].map(ht_by_date["power_factor"]) if len(ht) and "power_factor" in ht else np.nan
    return d


def as_lt_ht(daily: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Turn bill_daily rows into LT and HT frames shaped like the imported ones,
    so every existing calculation works unchanged."""
    lt = pd.DataFrame({
        "period": daily["date"].dt.strftime("%d %b"), "from_date": daily["date"], "to_date": daily["date"],
        "opening": np.nan, "closing": np.nan, "units": np.nan, "kwh": daily["lt_kwh"],
        "note": "estimated from bill",
    })
    ht = pd.DataFrame({
        "date": daily["date"], "day": daily["date"].dt.strftime("%a"), "import_kwh": daily["ht_import_kwh"],
        "export_kwh": daily["ht_export_kwh"], "net_import_kwh": daily["ht_net_kwh"],
        "power_factor": daily["power_factor"], "note": "estimated from bill",
    })
    return lt, ht
