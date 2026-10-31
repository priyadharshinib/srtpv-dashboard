"""Actuals View and Projected View.

Actuals    - inverter readings exactly as imported (blank stays blank).
Projected  - the report's split (core/projection.py): an inverter down most of the month gets
             the month's share of each day's total (Sep 10%, Oct 7%), the others are scaled down. Day and month totals
             are unchanged. Computed on the fly, never stored.

Who sees what:
  O&M Manager    (admin)         Actuals or Projected (switch in the sidebar)
  O&M Executive  (site_manager)  Actuals
  Customer       (customer)      Projected only, cannot switch

To stop using projections (e.g. once the inverters are repaired), set the secret
PROJECTED_VIEW_ENABLED = "false": everyone then sees Actuals.
"""
from __future__ import annotations

import pandas as pd

from core import config
from core.config import INVERTER_IDS
from core.projection import project
from core.settings import get_setting

ACTUALS = "Actuals"
PROJECTED = "Projected"


def projected_view_enabled() -> bool:
    override = get_setting("PROJECTED_VIEW_ENABLED") or get_setting("AVG_VIEW_ENABLED")
    if override is not None:
        return str(override).strip().lower() in ("1", "true", "yes", "on")
    return bool(getattr(config, "PROJECTED_VIEW_ENABLED", True))


def views_for(role: str) -> list[str]:
    """Views a role may use; the first one is the default."""
    if not projected_view_enabled():
        return [ACTUALS]
    if role == "admin":
        return [ACTUALS, PROJECTED]
    if role == "customer":
        return [PROJECTED]
    return [ACTUALS]


def apply_view(inv: pd.DataFrame, view: str, shares: dict | None = None) -> pd.DataFrame:
    """Inverter data for the chosen view. Adds proj_PV1..proj_PV9 flags (True = projected)."""
    if view != PROJECTED or inv.empty:
        out = inv.copy()
        for p in INVERTER_IDS:
            out[f"proj_{p}"] = False
        return out
    out, replaced = project(inv, shares)
    for p in INVERTER_IDS:
        out[f"proj_{p}"] = replaced[p].values
    return out
