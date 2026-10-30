"""Monthly HT bill: rupee formatting, demand headline and 'why the demand peaked'. Pure Python."""
from __future__ import annotations

import pandas as pd

from core.filters import weather_group


def inr(amount: float | None) -> str:
    """Indian digit grouping: 2979191 -> ₹29,79,191."""
    if amount is None or pd.isna(amount):
        return "—"
    n = round(amount)
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-" if n < 0 else "") + "₹" + s


def demand_pct(md_kva: float, contract_kva: float) -> float:
    """Maximum demand as % of contract, rounded to 0.1 (99.65 -> 99.6 as on the bill)."""
    return round(md_kva / contract_kva * 100 - 1e-9, 1)


def demand_headline(bill: dict, inv: pd.DataFrame) -> str:
    pct = demand_pct(bill["max_demand_kva"], bill["contract_demand_kva"])
    md_day = pd.Timestamp(bill["md_at"]).normalize()
    if pct > 100:
        text = f"Maximum demand exceeded contract demand by {pct - 100:.1f}%"
    elif pct >= 90:
        text = f"Maximum demand came within {100 - pct:.1f}% of contract demand"
    else:
        text = f"Maximum demand peaked at {pct:.1f}% of contract demand"
    day = inv.loc[inv["date"] == md_day]
    if not day.empty:
        if day["Total"].iloc[0] == inv["Total"].min():
            text += ", on the month's weakest solar day"
        elif day["Total"].iloc[0] < inv["Total"].median() * 0.5:
            text += f", on a low-solar day ({md_day:%d %b})"
        else:
            text += f", on {md_day:%d %b}"
    return text


def demand_story(bill: dict, inv: pd.DataFrame, month_name: str) -> str | None:
    """'Why the demand peaked' paragraph, or None if the MD day has no inverter data."""
    md_at = pd.Timestamp(bill["md_at"])
    day = inv.loc[inv["date"] == md_at.normalize()]
    if day.empty:
        return None
    kwh = float(day["Total"].iloc[0])
    median = float(inv["Total"].median())
    ratio = kwh / median if median else 1.0
    group = weather_group(str(day["weather"].iloc[0]))
    held = {"Rain / storm": "Rain held the inverters to",
            "Cloudy": "Cloud cover held the inverters to",
            "Partly cloudy": "Patchy cloud held the inverters to"}.get(group, "The inverters produced")
    load = "the whole morning load" if md_at.hour < 12 else "that day's load"
    if ratio < 0.25:
        tail = f"so almost {load} came from the grid."
    elif ratio < 0.6:
        tail = f"so most of {load.replace('the whole ', 'the ')} came from the grid."
    else:
        tail = "so solar output was normal; the peak came from site load itself."
    return (f"The {bill['max_demand_kva']:,.1f} kVA peak was set at {md_at:%H:%M} on {md_at.day} {md_at:%b}. "
            f"{held} {kwh:,.0f} kWh that day, against a {month_name} median of about {median:,.0f} kWh, {tail}")
