"""Projected inverter data (computed in the backend, never stored).

A reading is treated as faulty when it is blank, or below PROJECTION_THRESHOLD of the
average of the other inverters on the same roof. A faulty reading is replaced by the
average of the healthy inverters on that roof (e.g. PV4 = (PV5 + PV6) / 2 for RR College).
If no inverter on the roof is healthy that day (e.g. the whole roof is offline), the
reading is left as it is.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.config import INVERTER_IDS, PROJECTION_GROUPS, PROJECTION_THRESHOLD


def roof_siblings() -> dict[str, list[str]]:
    group_of = {p: g for g, members in PROJECTION_GROUPS.items() for p in members}
    return {p: [q for q in PROJECTION_GROUPS.get(group_of.get(p), []) if q != p] for p in INVERTER_IDS}


def project(inv: pd.DataFrame, threshold: float = PROJECTION_THRESHOLD) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (projected inv, boolean frame of replaced cells). Only PV1..PV9 and Total change.

    Pass 1 - low:  blank, or below threshold x the average of the others in the group.
    Pass 2 - high: above (1 / threshold) x the average of the group's non-low others
                   (e.g. one inverter's row also holding a neighbour's reading).
    Faulty readings are replaced by the average of the healthy inverters in the group.
    """
    sib = roof_siblings()
    vals = inv[INVERTER_IDS].astype(float)
    low = pd.DataFrame(False, index=inv.index, columns=INVERTER_IDS)
    for p in INVERTER_IDS:
        mean = vals[sib[p]].mean(axis=1)
        low[p] = vals[p].isna() | ((mean > 0) & (vals[p] < threshold * mean))
    high = pd.DataFrame(False, index=inv.index, columns=INVERTER_IDS)
    for p in INVERTER_IDS:
        ok_mean = vals[sib[p]].where(~low[sib[p]]).mean(axis=1)
        high[p] = ~low[p] & (ok_mean > 0) & (vals[p] > ok_mean / threshold)
    faulty = low | high
    out = inv.copy()
    replaced = pd.DataFrame(False, index=inv.index, columns=INVERTER_IDS)
    for p in INVERTER_IDS:
        base = vals[sib[p]].where(~faulty[sib[p]]).mean(axis=1)
        use = faulty[p] & base.notna()
        out.loc[use, p] = base[use].round(2)
        replaced[p] = use
    out["Total"] = out[INVERTER_IDS].sum(axis=1, min_count=1).round(2)
    return out, replaced
