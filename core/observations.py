"""Rule-based observations. Each returns (title, detail) in Markdown.

These replace the hand-written observations, so the report updates itself
every month. Add your own engineering notes in the app's notes box.
"""
from __future__ import annotations

import pandas as pd

from core.calculations import inverter_totals, outage_mask, single_day_lt
from core.config import INVERTERS, LOW_INVERTER_RATIO


def _d(ts: pd.Timestamp) -> str:
    return ts.strftime("%d %b")


def build_observations(inv: pd.DataFrame, lt: pd.DataFrame, ht: pd.DataFrame, kpis: dict) -> list[tuple[str, str]]:
    obs: list[tuple[str, str]] = []

    # 1. Chronically low inverters
    totals = inverter_totals(inv)
    for _, r in totals[totals["vs_others"] < LOW_INVERTER_RATIO].iterrows():
        same_roof = [p for p, loc in INVERTERS.items() if loc == r["location"] and p != r["inverter"]]
        roof_ok = all(totals.set_index("inverter").loc[same_roof, "vs_others"] >= 0.9) if same_roof else False
        zero = f" Zero output on {', '.join(r['zero_days'])}." if r["zero_days"] else ""
        fault = f" {', '.join(same_roof)} on the same roof are normal, so the fault is specific to {r['inverter']}." if roof_ok else ""
        obs.append((
            f"{r['inverter']} ({r['location']}) is chronically low.",
            f"{r['kwh']:,.0f} kWh month to date, only {r['vs_others']:.0%} of the other inverters' average.{zero}{fault} Send to O&M.",
        ))

    # 2. Outage days
    for _, r in inv[outage_mask(inv)].iterrows():
        obs.append((
            f"{_d(r['date'])} outage.",
            f"Inverters logged only {r['Total']:,.0f} kWh ({r['weather'] or 'weather not recorded'}). "
            "Check inverter logs, grid availability and any BESCOM shutdown.",
        ))

    # 3. Inverter total vs LT on single-day periods: incomplete data or logging gaps
    lt_day = single_day_lt(lt).merge(inv[["date", "Total"]], on="date")
    for _, r in lt_day.iterrows():
        ratio = r["kwh"] / r["Total"] if r["Total"] else float("inf")
        if ratio > 1.5:
            obs.append((
                f"{_d(r['date'])} inverter figure looks incomplete.",
                f"Inverters show {r['Total']:,.0f} kWh but the LT meter recorded {r['kwh']:,.0f} kWh. "
                "Re-export the inverter data after sunset.",
            ))
        elif ratio > 1.0:
            obs.append((
                f"{_d(r['date'])} monitoring gap.",
                f"LT meter ({r['kwh']:,.0f} kWh) is higher than the inverter total ({r['Total']:,.0f} kWh). "
                "This points to missing monitoring data, not lost generation.",
            ))

    # 4. Export days
    exp_days = ht[ht["export_kwh"] > 0]
    if not exp_days.empty:
        days = ", ".join(f"{_d(d)} ({w})" for d, w in zip(exp_days["date"], exp_days["day"]))
        obs.append((
            "Grid export.",
            f"{kpis['ht_export_kwh']:,.0f} kWh exported on {days}. On other days all solar was consumed on site.",
        ))

    # 5. LT vs inverter
    obs.append((
        "LT meter vs inverters.",
        f"Up to {_d(kpis['compare_through'])} the LT meter reads {kpis['lt_vs_inverter']:+.1%} against the inverter total. "
        "Normally expect 2–4% lower (AC and cable losses); a positive figure usually means inverter logging gaps.",
    ))
    return obs


def takeaway(kpis: dict, inv: pd.DataFrame) -> str:
    last = inv["date"].max()
    text = (
        f"Inverters logged **{kpis['inverter_kwh']:,.0f} kWh** up to {_d(last)}. "
        f"The LT meter recorded **{kpis['lt_kwh']:,.0f} kWh**, covering "
        f"**{kpis['solar_share']:.0%}** of site consumption."
    )
    outages = inv.loc[outage_mask(inv), "date"]
    if not outages.empty:
        text += f" The plant was largely off on {', '.join(_d(d) for d in outages)}."
    return text


def inverter_issues(inv: pd.DataFrame, period_word: str = "month", only: list[str] | None = None) -> list[str]:
    """Clauses such as 'PV4 was offline for most of the month' (empty in the Avg View)."""
    from core.calculations import flagged_cells
    from core.config import INVERTER_IDS, OFFLINE_FRACTION

    out, n = [], len(inv)
    if not n:
        return out
    day_median = inv[INVERTER_IDS].median(axis=1)
    offline = inv[INVERTER_IDS].lt(day_median * OFFLINE_FRACTION, axis=0)
    weak = flagged_cells(inv)
    ratios = inverter_totals(inv).set_index("inverter")["vs_others"]
    for p in (only or INVERTER_IDS):
        if offline[p].sum() >= n / 2:
            out.append(f"{p} was offline for most of the {period_word}")
        elif weak[p].sum() >= n / 2:
            out.append(f"{p} ran well below the other inverters for most of the {period_word}")
        elif ratios[p] < LOW_INVERTER_RATIO:
            out.append(f"{p} produced {ratios[p]:.0%} of the other inverters' average")
    return out


def glance_headline(kpis: dict, inv: pd.DataFrame, period_word: str = "month",
                    include_outages: bool = True, only: list[str] | None = None) -> str:
    """'Solar covered 23% of site consumption; PV4 was offline for most of the month'."""
    parts = [f"Solar covered {kpis['solar_share']:.0%} of site consumption", *inverter_issues(inv, period_word, only)]
    outages = inv.loc[outage_mask(inv), "date"]
    if include_outages and not outages.empty:
        parts.append(f"the plant was largely off on {', '.join(_d(d) for d in outages)}")
    return "; ".join(parts)
