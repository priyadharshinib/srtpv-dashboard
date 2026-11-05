"""Pieces of the monthly report layout (pure pandas, no Streamlit)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.billing import inr
from core.config import INVERTER_IDS, PF_LIMIT, VERY_LOW_FRACTION
from core.estimation import as_lt_ht, bill_daily, bill_has_totals


def short_weather(text: str) -> str:
    """'Partly cloudy / Thundershower' -> 'Thunder', 'Partly cloudy / Mainly sunny' -> 'Sunny spells'."""
    s = (text or "").lower()
    if not s.strip() or s.strip() in ("—", "-"):
        return "No data"
    if "thunder" in s:
        return "Thunder"
    if any(k in s for k in ("rain", "shower", "drizzle")):
        return "Rain"
    if "bright" in s:
        return "Bright"
    if "sunny" in s or "clear" in s:
        return "Sunny spells"
    if "overcast" in s:
        return "Overcast"
    if "partly" in s:
        return "Partly cloudy"
    if "cloud" in s:
        return "Cloudy"
    return text.strip().split("/")[0].strip().capitalize()


def very_low_mask(totals: pd.Series) -> pd.Series:
    return totals < totals.median() * VERY_LOW_FRACTION


def _days(dates) -> str:
    ds = [f"{d:%d}" for d in dates]
    return ds[0] if len(ds) == 1 else ", ".join(ds[:-1]) + " and " + ds[-1]


def day_ranges(dates) -> str:
    """Consecutive days joined up: [14..30 Oct] -> '14–30 Oct'; [3, 7, 8] -> '03 Oct, 07–08 Oct'."""
    ds = sorted(pd.to_datetime(list(dates)))
    runs: list[list[pd.Timestamp]] = []
    for d in ds:
        if runs and (d - runs[-1][-1]).days == 1:
            runs[-1].append(d)
        else:
            runs.append([d])
    out = []
    for r in runs:
        a, b = r[0], r[-1]
        if a == b:
            out.append(f"{a:%d %b}")
        elif (a.year, a.month) == (b.year, b.month):
            out.append(f"{a:%d}–{b:%d %b}")
        else:
            out.append(f"{a:%d %b} – {b:%d %b}")
    return ", ".join(out)


def daily_headline(inv: pd.DataFrame, nodata: list) -> str:
    if inv.empty:
        return ""
    mon = inv["date"].iloc[0].strftime("%b")
    best = inv.nlargest(2, "Total")["date"].tolist()
    parts = [f"Best days were {_days(best)} {mon}"]
    low = inv[very_low_mask(inv["Total"])]
    if not low.empty:
        wet = all(short_weather(w) in ("Rain", "Thunder") for w in low["weather"])
        parts.append(f"rain on {_days(low['date'])} {mon} cut output sharply" if wet
                     else f"output fell sharply on {_days(low['date'])} {mon}")
    text = "; ".join(parts) + "."
    if nodata:
        text += f" {_days(nodata)} {nodata[0]:%b} has no inverter data." if len(nodata) == 1 else \
                f" {_days(nodata)} {nodata[0]:%b} have no inverter data."
    return text


def month_list(start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    return [p.strftime("%Y-%m") for p in pd.period_range(start, end, freq="M")]


def fill_bill_lt(bills: dict, lt: pd.DataFrame) -> dict:
    """A bill saved without the SRTPV (LT) meter units has no LT total, so the dashboard would ignore its
    HT import / export. Use the month's imported LT meter readings as that total instead (bills are not modified)."""
    out = {}
    for m, b in bills.items():
        if b and b.get("lt_kwh") is None and b.get("ht_import_kwh") is not None and b.get("ht_export_kwh") is not None:
            m_start = pd.Timestamp(m + "-01")
            m_end = m_start + pd.offsets.MonthEnd(0)
            lt_m = lt[(lt["from_date"] >= m_start) & (lt["from_date"] <= m_end)] if len(lt) else lt
            if len(lt_m):
                b = {**b, "lt_kwh": float(lt_m["kwh"].sum())}
        out[m] = b
    return out


def bill_totals_fallback(kpis: dict, bills: dict, bill_months: list[str], ht: pd.DataFrame,
                         start: pd.Timestamp, end: pd.Timestamp) -> tuple[dict, list[str]]:
    """At-a-glance figures for a month that has the HT bill totals but no HT meter readings.

    Used only for a month that (a) is not already bill-based (no LT total, so build_period could not use
    the bill), (b) has no daily HT readings, (c) is fully inside the period, and (d) has the bill's total
    HT import and export. Its totals are added to the KPIs; every other month and every figure of a month
    that has HT readings are left exactly as they were. Returns (kpis, months_used).
    """
    out, used = dict(kpis), []
    for m in month_list(start, end):
        b = bills.get(m)
        if m in bill_months or not b or b.get("ht_import_kwh") is None or b.get("ht_export_kwh") is None:
            continue
        m_start = pd.Timestamp(m + "-01")
        m_end = m_start + pd.offsets.MonthEnd(0)
        if start > m_start or end < m_end:
            continue
        if len(ht) and ht["date"].between(m_start, m_end).any():
            continue
        imp, exp = float(b["ht_import_kwh"]), float(b["ht_export_kwh"])
        out["ht_import_kwh"] += imp
        out["ht_export_kwh"] += exp
        out["ht_net_kwh"] += imp - exp
        out["site_load_kwh"] += imp - float(b.get("bill_lt_kwh") or 0.0) - exp   # LT solar is already in the KPI
        used.append(m)
    if used:
        out["solar_share"] = out["lt_kwh"] / out["site_load_kwh"] if out["site_load_kwh"] else 0.0
    return out, used


def build_period(inv: pd.DataFrame, lt: pd.DataFrame, ht: pd.DataFrame, bills: dict[str, dict | None],
                 start: pd.Timestamp, end: pd.Timestamp):
    """Combine imported data and bill totals for a period.

    Returns (lt, ht, daily, nodata_dates, bill_months):
      lt/ht  - frames for the existing KPI functions (bill months replaced by bill estimates)
      daily  - one row per calendar day: weather, inverter, LT, HT, site load, flags
    """
    lt_parts, ht_parts, daily_parts, bill_months = [], [], [], []
    weather = inv.set_index("date")[["day", "weather", "temp_c", "wind_ms"]] if len(inv) else None
    for m in month_list(start, end):
        m_start = pd.Timestamp(m + "-01")
        m_end = m_start + pd.offsets.MonthEnd(0)
        inv_m = inv[(inv["date"] >= m_start) & (inv["date"] <= m_end)]
        ht_m = ht[(ht["date"] >= m_start) & (ht["date"] <= m_end)]
        lt_m = lt[(lt["from_date"] >= m_start) & (lt["from_date"] <= m_end)]
        bill = bills.get(m)
        if bill_has_totals(bill):
            d = bill_daily(inv_m, ht_m, bill, m)
            d["estimated"] = True
            l, h = as_lt_ht(d)
            lt_parts.append(l); ht_parts.append(h); bill_months.append(m)
        else:
            lt_parts.append(lt_m); ht_parts.append(ht_m)
            last = max([x for x in (inv_m["date"].max(), ht_m["date"].max() if len(ht_m) else pd.NaT) if pd.notna(x)],
                       default=m_start)
            days = pd.date_range(m_start, last)
            d = pd.DataFrame({"date": days})
            d = d.merge(inv_m[["date", "Total"]].rename(columns={"Total": "inverter_kwh"}), on="date", how="left")
            # LT per day: single-day readings as they are; a multi-day reading (e.g. a weekend)
            # is split across its days in proportion to inverter output, so the days add up
            # to the meter reading.
            lt_day: dict = {}
            inv_tot = inv_m.set_index("date")["Total"] if len(inv_m) else pd.Series(dtype=float)
            for r in lt_m.itertuples():
                days_r = pd.date_range(r.from_date, r.to_date)
                w = pd.Series([float(inv_tot.get(x, np.nan)) for x in days_r], index=days_r)
                w = w.fillna(0.0)
                w = w / w.sum() if w.sum() > 0 else pd.Series(1 / len(days_r), index=days_r)
                for x in days_r:
                    lt_day[x] = float(r.kwh) * w[x]
            d["lt_kwh"] = d["date"].map(lt_day)
            hb = ht_m.set_index("date")
            for col, src in (("ht_import_kwh", "import_kwh"), ("ht_export_kwh", "export_kwh"),
                             ("ht_net_kwh", "net_import_kwh"), ("power_factor", "power_factor")):
                d[col] = d["date"].map(hb[src]) if len(hb) else np.nan
            d["site_load_kwh"] = d["ht_net_kwh"] + d["lt_kwh"]
            d["no_data"] = d["inverter_kwh"].isna()
            d["inverter_est"] = np.nan
            d["estimated"] = False
        daily_parts.append(d)
    daily = pd.concat(daily_parts, ignore_index=True)
    daily = daily[(daily["date"] >= start) & (daily["date"] <= end)].reset_index(drop=True)
    daily["day"] = daily["date"].dt.strftime("%a")
    if weather is not None:
        for c in ("weather", "temp_c", "wind_ms"):
            daily[c] = daily["date"].map(weather[c])
    else:
        daily["weather"], daily["temp_c"], daily["wind_ms"] = "", np.nan, np.nan
    daily["weather"] = daily["weather"].fillna("")
    lo = pd.concat(lt_parts, ignore_index=True) if lt_parts else lt
    ho = pd.concat(ht_parts, ignore_index=True) if ht_parts else ht
    # combining months with and without data can turn date columns into plain objects
    for frame, cols in ((lo, ("from_date", "to_date")), (ho, ("date",))):
        for c in cols:
            frame[c] = pd.to_datetime(frame[c])
    for c in ("kwh", "opening", "closing", "units"):
        if c in lo:
            lo[c] = pd.to_numeric(lo[c], errors="coerce")
    for c in ("import_kwh", "export_kwh", "net_import_kwh", "power_factor"):
        if c in ho:
            ho[c] = pd.to_numeric(ho[c], errors="coerce")
    lo = lo[(lo["from_date"] >= start) & (lo["from_date"] <= end)].reset_index(drop=True)
    ho = ho[(ho["date"] >= start) & (ho["date"] <= end)].reset_index(drop=True)
    nodata = daily.loc[daily["no_data"], "date"].tolist()
    return lo, ho, daily, nodata, bill_months


def followups(month_name: str | None, bill: dict | None, grid: dict, glance_inverter_issues: list[str],
              nodata: list, n_estimated_cells: int) -> list[tuple[str, str]]:
    """'Observations and follow-up' items (title, text)."""
    items = []
    if bill and bill.get("avg_pf") is not None and bill["avg_pf"] < PF_LIMIT:
        pen = f" drew a {inr(bill['pf_penalty'])} penalty" if bill.get("pf_penalty") else " is below the limit"
        items.append(("Correct the power factor",
                      f"{month_name} average {bill['avg_pf']:.3f}{pen}. Add APFC compensation or enable "
                      "reactive-power support on the inverters during solar hours."))
    elif grid.get("pf_low_days"):
        items.append(("Correct the power factor",
                      f"Power factor fell below {PF_LIMIT:.2f} on {', '.join(grid['pf_low_days'])}. "
                      "Check the APFC panel and capacitor stages."))
    if bill and bill.get("max_demand_kva") and bill.get("contract_demand_kva"):
        pct = bill["max_demand_kva"] / bill["contract_demand_kva"]
        if pct >= 0.95:
            items.append(("Keep maximum demand under the contract",
                          f"Peak reached {pct:.1%} of the {bill['contract_demand_kva']:,.0f} kVA contract. "
                          "On low-solar mornings, stagger large loads such as chillers and pumps."))
    for issue in glance_inverter_issues:
        items.append(("Inverter follow-up", issue[0].upper() + issue[1:] + ". Inspect and confirm with the inverter portal."))
    if nodata:
        items.append(("Restore inverter monitoring",
                      f"No inverter data for {', '.join(f'{d:%d %b}' for d in nodata)}. "
                      "Check the data logger and internet link so the next report is complete."))
    if n_estimated_cells:
        items.append(("Projected inverter split",
                      f"On {n_estimated_cells} inverter-day(s) a share of the day's total is shown on an inverter "
                      "that was down; marked p in the table. Day and month totals are the actual figures."))
    return items
