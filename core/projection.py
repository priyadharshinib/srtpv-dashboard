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

From PEER_CHECK_FROM (1 Oct 2026) a reading is also faulty when it is more than PEER_TOLERANCE_KWH (50)
above or below the median of the inverters at the same place (PEER_GROUPS: PV1-3, PV4-6, PV7-9).
Days before that date, so August and September 2026, are judged by the original rule only and come out
exactly as before.

From October 2026 the fill itself also changes (see _fill_day_peers). On a day when a chronically faulty
inverter's own reading is faulty, it is filled with the average of the healthy inverters at the same place
(so it ends up within PEER_TOLERANCE_KWH of them, e.g. PV4 next to PV5 and PV6), and every other reading is
scaled by one common factor so the day total is still exactly the actual total. A faulty inverter's healthy
days are left as recorded. If no healthy inverter at the same place is left that day, the original
share method is used for that day. Months before October 2026 always use the original share method.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.config import (INVERTER_IDS, PEER_CHECK_FROM, PEER_GROUPS, PEER_TOLERANCE_KWH, PROJECTION_GROUPS,
                         PROJECTION_MIN_FAULTY_DAYS, PROJECTION_SHARE, PROJECTION_THRESHOLD)


def _r2(x: float) -> float:
    """Round to 2 decimals on the binary value, as Excel's ROUND does on computed numbers
    (e.g. 333.15 x 0.9 is stored as 299.83499... and rounds to 299.83, as in the report)."""
    return round(float(x), 2)


def roof_siblings() -> dict[str, list[str]]:
    group_of = {p: g for g, members in PROJECTION_GROUPS.items() for p in members}
    return {p: [q for q in PROJECTION_GROUPS.get(group_of.get(p), []) if q != p] for p in INVERTER_IDS}


def peer_outliers(inv: pd.DataFrame) -> pd.DataFrame:
    """True where a reading is more than PEER_TOLERANCE_KWH above or below the median of the inverters at the
    same place (PEER_GROUPS) that day. Only for days from PEER_CHECK_FROM; earlier days are always False.
    A group needs at least 3 readings that day for the comparison to mean anything."""
    out = pd.DataFrame(False, index=inv.index, columns=INVERTER_IDS)
    if inv.empty or "date" not in inv.columns:
        return out
    active = (pd.to_datetime(inv["date"]) >= pd.Timestamp(PEER_CHECK_FROM)).to_numpy()
    if not active.any():
        return out
    vals = inv[INVERTER_IDS].astype(float)
    for members in PEER_GROUPS.values():
        members = [p for p in members if p in INVERTER_IDS]
        sub = vals[members]
        median = sub.median(axis=1)
        enough = (sub.notna().sum(axis=1) >= 3).to_numpy()
        for p in members:
            out[p] = active & enough & ((sub[p] - median).abs() > PEER_TOLERANCE_KWH).to_numpy()
    return out


def faulty_cells(inv: pd.DataFrame, threshold: float = PROJECTION_THRESHOLD) -> pd.DataFrame:
    """True where a reading is blank or below threshold x the average of its group's others.
    From PEER_CHECK_FROM (Oct 2026) it is also True where the reading is far from its peers (peer_outliers)."""
    sib = roof_siblings()
    vals = inv[INVERTER_IDS].astype(float)
    out = pd.DataFrame(False, index=inv.index, columns=INVERTER_IDS)
    for p in INVERTER_IDS:
        mean = vals[sib[p]].mean(axis=1)
        out[p] = vals[p].isna() | ((mean > 0) & (vals[p] < threshold * mean))
    return out | peer_outliers(inv)


def chronic_inverters(inv: pd.DataFrame, faulty: pd.DataFrame | None = None) -> dict[str, list[str]]:
    """{'2026-09': ['PV4'], ...}: inverters faulty on at least the set share of a month's days."""
    faulty = faulty_cells(inv) if faulty is None else faulty
    months = inv["date"].dt.strftime("%Y-%m")
    return {m: [p for p in INVERTER_IDS if faulty.loc[idx, p].mean() >= PROJECTION_MIN_FAULTY_DAYS]
            for m, idx in months.groupby(months).groups.items()}


def peer_fill_month(month: str) -> bool:
    """True for months that use the peer-based fill (October 2026 onwards); earlier months use the share method."""
    return month >= PEER_CHECK_FROM[:7]


def _group_of() -> dict[str, list[str]]:
    return {p: [q for q in members if q in INVERTER_IDS] for members in PEER_GROUPS.values() for p in members}


def _fill_day_peers(vals_row: pd.Series, faulty_row: pd.Series, targets: list[str], total: float):
    """Peer-based fill for one day, or None when it cannot be done (then the share method is used).

    fill = chronic inverters whose reading is faulty today. Each gets the mean of the healthy readings of the
    other inverters at the same place; with k = day total / (sum of all non-filled readings + the filled values
    before scaling), every value is multiplied by k so the nine values still add up to the actual day total.
    Returns ({inverter: new value or None}, [filled inverters])."""
    group = _group_of()
    fill = [t for t in targets if faulty_row[t]]
    if not fill:
        return {}, []                                # nothing faulty on this day: left exactly as recorded
    want = {}
    for t in fill:
        peers = [q for q in group.get(t, []) if q != t and q not in fill and not pd.isna(vals_row[q]) and not faulty_row[q]]
        if not peers:
            return None
        want[t] = float(np.mean([vals_row[q] for q in peers]))
    kept = {p: vals_row[p] for p in INVERTER_IDS if p not in fill and not pd.isna(vals_row[p])}
    base = sum(kept.values()) + sum(want.values())
    if base <= 0:
        return None
    k = total / base
    new = {p: (None if pd.isna(vals_row[p]) else _r2(vals_row[p] * k)) for p in INVERTER_IDS if p not in fill}
    for t in fill[:-1]:
        new[t] = _r2(want[t] * k)
    last = fill[-1]                                  # takes the rounding remainder, so the nine values add up
    new[last] = _r2(total - sum(v for v in new.values() if v is not None))
    return new, fill


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
    faulty = faulty_cells(inv)
    for m, targets in chronic_inverters(inv, faulty).items():
        if not targets:
            continue
        rows = months[months == m].index
        share = share_for(m, shares)
        scale = round(1 - share * len(targets), 10)      # 0.93 exactly, as the spreadsheet uses
        for i in rows:
            if pd.isna(totals[i]):
                continue
            if peer_fill_month(m):                       # October 2026 onwards: match the inverters at the same place
                done = _fill_day_peers(vals.loc[i], faulty.loc[i], targets, float(totals[i]))
                if done is not None:
                    new, filled = done
                    for p, v in new.items():                 # empty when the day needs no fill
                        out.at[i, p] = np.nan if v is None else v
                    for t in filled:
                        marked.at[i, t] = True
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
