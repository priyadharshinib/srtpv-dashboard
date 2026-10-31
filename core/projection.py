"""Projected inverter data (computed in the backend, never stored).

Method (as in the monthly report): totals never change.
1. Find each month's chronically faulty inverters: faulty (blank, or below
   PROJECTION_THRESHOLD of the average of the other inverters in its group) on at least
   PROJECTION_MIN_FAULTY_DAYS of the month's days. In September 2026 that is PV4.
2. On every day of that month, each such inverter gets the month's share of the day's
   actual total, and every inverter's actual reading is scaled by
   1 - (share x number of such inverters). The share is PROJECTION_SHARE (10%) unless a
   different share was saved for that month in the app (the October 2026 report used 7%).
   With 10%: PV4 shown = 0.9 x PV4 actual + 0.1 x day total; others = 0.9 x actual.
3. Each value is rounded to 2 decimals; the receiving inverter is the day total minus the other
   rounded values (as in the report), so the nine values add up exactly to the actual day total.
Months without a chronically faulty inverter are left exactly as recorded.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.config import (INVERTER_IDS, PROJECTION_GROUPS, PROJECTION_MIN_FAULTY_DAYS,
                         PROJECTION_SHARE, PROJECTION_THRESHOLD)


def _r2(x: float) -> float:
    """Round to 2 decimals on the binary value, as Excel's ROUND does on computed numbers
    (e.g. 333.15 x 0.9 is stored as 299.83499... and rounds to 299.83, as in the report)."""
    return round(float(x), 2)


def roof_siblings() -> dict[str, list[str]]:
    group_of = {p: g for g, members in PROJECTION_GROUPS.items() for p in members}
    return {p: [q for q in PROJECTION_GROUPS.get(group_of.get(p), []) if q != p] for p in INVERTER_IDS}


def faulty_cells(inv: pd.DataFrame, threshold: float = PROJECTION_THRESHOLD) -> pd.DataFrame:
    """True where a reading is blank or below threshold x the average of its group's others."""
    sib = roof_siblings()
    vals = inv[INVERTER_IDS].astype(float)
    out = pd.DataFrame(False, index=inv.index, columns=INVERTER_IDS)
    for p in INVERTER_IDS:
        mean = vals[sib[p]].mean(axis=1)
        out[p] = vals[p].isna() | ((mean > 0) & (vals[p] < threshold * mean))
    return out


def chronic_inverters(inv: pd.DataFrame, faulty: pd.DataFrame | None = None) -> dict[str, list[str]]:
    """{'2026-09': ['PV4'], ...}: inverters faulty on at least the set share of a month's days."""
    faulty = faulty_cells(inv) if faulty is None else faulty
    months = inv["date"].dt.strftime("%Y-%m")
    return {m: [p for p in INVERTER_IDS if faulty.loc[idx, p].mean() >= PROJECTION_MIN_FAULTY_DAYS]
            for m, idx in months.groupby(months).groups.items()}


def share_for(month: str, shares: dict | None = None) -> float:
    """The month's share: the value saved in the app for that month, else PROJECTION_SHARE."""
    if shares and month in shares:
        return float(shares[month])
    return float(PROJECTION_SHARE)


def project(inv: pd.DataFrame, shares: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (projected inv, boolean frame of the cells that receive a share).
    Day totals are unchanged; only the split between inverters changes."""
    out = inv.copy()
    marked = pd.DataFrame(False, index=inv.index, columns=INVERTER_IDS)
    if inv.empty:
        return out, marked
    vals = inv[INVERTER_IDS].astype(float)
    totals = vals.sum(axis=1, min_count=1)
    months = inv["date"].dt.strftime("%Y-%m")
    for m, targets in chronic_inverters(inv).items():
        if not targets:
            continue
        rows = months[months == m].index
        share = share_for(m, shares)
        scale = round(1 - share * len(targets), 10)      # 0.93 exactly, as the spreadsheet uses
        for i in rows:
            if pd.isna(totals[i]):
                continue
            new = {p: (None if pd.isna(vals.at[i, p]) else _r2(vals.at[i, p] * scale))
                   for p in INVERTER_IDS if p not in targets}
            for t in targets[:-1]:
                new[t] = _r2((0 if pd.isna(vals.at[i, t]) else vals.at[i, t]) * scale + share * totals[i])
            last = targets[-1]                 # takes the rounding remainder, so the nine values
            new[last] = _r2(totals[i] - sum(v for v in new.values() if v is not None))   # add up exactly
            for p, v in new.items():
                out.at[i, p] = np.nan if v is None else v
            for t in targets:
                marked.at[i, t] = True
        out.loc[rows, "Total"] = totals[rows].round(2)          # always the actual day total
    return out, marked
