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


def pv_comparison(inv: pd.DataFrame) -> pd.DataFrame:
    """Per PV system over the days in `inv`: kWh and kWh per kWp (as in the inverter portal's
    'PV system comparison'). One row per inverter, in PV1..PV9 order."""
    from core.config import INVERTER_KWP, PV_SYSTEM_NAMES

    out = pd.DataFrame({
        "inverter": INVERTER_IDS,
        "name": [PV_SYSTEM_NAMES[p] for p in INVERTER_IDS],
        "kwp": [INVERTER_KWP[p] for p in INVERTER_IDS],
        "kwh": inv[INVERTER_IDS].sum().values,
    })
    out["kwh_per_kwp"] = out["kwh"] / out["kwp"]
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


# ---------------------------------------------------------------------
# Backend user: AC loss between inverters and LT meter
# ---------------------------------------------------------------------

def ac_losses(inv: pd.DataFrame, lt: pd.DataFrame) -> pd.DataFrame:
    """One row per LT period: inverter kWh vs LT kWh over the same days.

    loss_kwh = inverter - LT  (energy lost on the AC run from inverters to the LT panel)
    A negative loss means the LT meter read MORE than the inverters, which points to
    missing inverter monitoring data rather than a real gain.
    """
    from core.config import AC_LOSS_EXPECTED

    lo, hi = AC_LOSS_EXPECTED
    rows = []
    by_date = inv.set_index("date")["Total"]
    for r in lt.itertuples():
        days = pd.date_range(r.from_date, r.to_date)
        if not all(d in by_date.index for d in days):
            continue                      # inverter data missing for part of this period
        inv_kwh = float(by_date.loc[days].sum())
        loss = inv_kwh - float(r.kwh)
        pct = loss / inv_kwh if inv_kwh else float("nan")
        if pct < 0:
            status = "LT higher: inverter data gap"
        elif pct < lo:
            status = "Below expected"
        elif pct <= hi:
            status = "Normal"
        else:
            status = "High loss: check AC side"
        rows.append({"period": r.period, "from_date": r.from_date, "to_date": r.to_date, "days": len(days),
                     "inverter_kwh": inv_kwh, "lt_kwh": float(r.kwh), "loss_kwh": loss,
                     "loss_pct": pct, "status": status})
    return pd.DataFrame(rows, columns=["period", "from_date", "to_date", "days", "inverter_kwh",
                                       "lt_kwh", "loss_kwh", "loss_pct", "status"])


def ac_loss_totals(losses: pd.DataFrame) -> dict:
    """Totals over all periods, plus 'clean' totals that leave out inverter data-gap periods
    (where the LT meter reads higher), which otherwise hide the real AC loss."""
    def _tot(df):
        i, l = float(df["inverter_kwh"].sum()), float(df["lt_kwh"].sum())
        return i, l, i - l, ((i - l) / i if i else float("nan"))

    i, l, k, p = _tot(losses)
    clean = losses[losses["loss_kwh"] >= 0]
    ci, cl, ck, cp = _tot(clean)
    return {"inverter_kwh": i, "lt_kwh": l, "loss_kwh": k, "loss_pct": p, "periods": len(losses),
            "clean_inverter_kwh": ci, "clean_lt_kwh": cl, "clean_loss_kwh": ck, "clean_loss_pct": cp,
            "clean_periods": len(clean), "gap_periods": len(losses) - len(clean)}


# ---------------------------------------------------------------------
# Backend user: EB (grid) import/export, generation and power factor
# ---------------------------------------------------------------------

def grid_summary(ht: pd.DataFrame, lt: pd.DataFrame) -> dict:
    from core.config import PF_LIMIT

    pf = ht["power_factor"].dropna() if "power_factor" in ht.columns else pd.Series(dtype=float)
    generated = float(lt["kwh"].sum())
    eb_import = float(ht["import_kwh"].sum())
    export = float(ht["export_kwh"].sum())
    net = eb_import - export
    load = net + generated
    return {
        "generated_kwh": generated,
        "eb_import_kwh": eb_import,
        "export_kwh": export,
        "net_eb_kwh": net,
        "site_load_kwh": load,
        "solar_share": generated / load if load else 0.0,
        "export_share_of_generation": export / generated if generated else 0.0,
        "pf_avg": float(pf.mean()) if len(pf) else None,
        "pf_min": float(pf.min()) if len(pf) else None,
        "pf_days_recorded": int(len(pf)),
        "pf_low_days": ht.loc[ht["power_factor"] < PF_LIMIT, "date"].dt.strftime("%d %b").tolist()
        if "power_factor" in ht.columns else [],
    }


# ---------------------------------------------------------------------
# Customer: averages only
# ---------------------------------------------------------------------

def customer_averages(inv: pd.DataFrame, lt: pd.DataFrame, ht: pd.DataFrame) -> dict:
    from core.config import PLANT_CAPACITY_KWP

    n_inv = len(INVERTER_IDS)
    days = len(inv)
    g = grid_summary(ht, lt)
    return {
        "days": days,
        "avg_daily_generation": float(inv["Total"].mean()) if days else 0.0,
        "avg_per_inverter_daily": float(inv["Total"].mean() / n_inv) if days else 0.0,
        "avg_yield": float(inv["Total"].mean() / PLANT_CAPACITY_KWP) if days else 0.0,
        "avg_daily_eb_import": float(ht["import_kwh"].mean()) if len(ht) else 0.0,
        "solar_share": g["solar_share"],
        "total_generation": float(inv["Total"].sum()),
    }
