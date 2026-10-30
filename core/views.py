"""Actuals View and Avg View  (AVG-VIEW: temporary).

The Avg View replaces each inverter's daily kWh with that day's plant total
divided equally across the inverters. The plant Total is unchanged, so every
other figure (KPIs, LT, EB, AC loss, productivity, charts) stays identical.

Who sees what:
  O&M Manager    (admin)         Actuals or Avg (switch in the sidebar)
  O&M Executive  (site_manager)  Actuals
  Customer       (customer)      Avg only, cannot switch

To retire the Avg View later:
  1. quick:     set AVG_VIEW_ENABLED = "false" in Secrets (everyone sees Actuals), or
  2. permanent: delete this file and the lines marked AVG-VIEW in app.py.
"""
from __future__ import annotations

import pandas as pd

from core import config
from core.config import INVERTER_IDS
from core.settings import get_setting

ACTUALS = "Actuals"
AVG = "Avg"


def avg_view_enabled() -> bool:
    override = get_setting("AVG_VIEW_ENABLED")
    if override is not None:
        return str(override).strip().lower() in ("1", "true", "yes", "on")
    return bool(config.AVG_VIEW_ENABLED)


def views_for(role: str) -> list[str]:
    """Views a role may use; the first one is the default."""
    if not avg_view_enabled():
        return [ACTUALS]
    if role == "admin":
        return [ACTUALS, AVG]
    if role == "customer":
        return [AVG]
    return [ACTUALS]


def apply_view(inv: pd.DataFrame, view: str) -> pd.DataFrame:
    """Return inverter data for the chosen view. Only PV1..PV9 change."""
    if view != AVG:
        return inv
    out = inv.copy()
    share = out["Total"] / len(INVERTER_IDS)
    for p in INVERTER_IDS:
        out[p] = share
        if f"est_{p}" in out.columns:
            out[f"est_{p}"] = False
    return out
