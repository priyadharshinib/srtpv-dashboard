"""SRTPV plant monthly generation & grid report.

Run:  streamlit run app.py
"""
from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from core import charts, db
from core.calculations import (
    daily_statement,
    flagged_cells,
    headline_kpis,
    inverter_totals,
    outage_mask,
)
from core.config import INVERTER_IDS, LOW_INVERTER_RATIO, PLANT_CAPACITY_KWP, PLANT_NAME
from core.importer import import_ht_meter, import_inverter, import_lt_meter, load_sample
from core.observations import build_observations, takeaway
from core.settings import get_setting

st.set_page_config(page_title=f"{PLANT_NAME} report", page_icon="☀️", layout="wide")


@st.cache_resource
def _prepare_database() -> bool:
    """Create the schema and tables once per server start, not on every click."""
    db.init_db()
    return True


try:
    _prepare_database()
except Exception as e:
    st.error(f"Could not connect to the database: {e}")
    st.info("Check DATABASE_URL in .streamlit/secrets.toml (local) or in the app's Secrets (Streamlit Cloud).")
    st.stop()

GRAFANA_URL = get_setting("GRAFANA_URL")


# ---------------- Sidebar: import ----------------
with st.sidebar:
    st.header("Import data")
    f_inv = st.file_uploader("Inverter daily readings (PV1–PV9)", type=["xlsx", "csv"])
    f_lt = st.file_uploader("LT generation meter", type=["xlsx", "csv"])
    f_ht = st.file_uploader("HT import / export meter", type=["xlsx", "csv"])

    if st.button("Validate and save", type="primary", disabled=not (f_inv or f_lt or f_ht)):
        jobs = [(f_inv, import_inverter, db.save_inverter, "Inverter"),
                (f_lt, import_lt_meter, db.save_lt, "LT meter"),
                (f_ht, import_ht_meter, db.save_ht, "HT meter")]
        for file, importer, saver, label in jobs:
            if file is None:
                continue
            try:
                res = importer(file)
            except Exception as e:  # unreadable file
                st.error(f"{label}: {e}")
                continue
            for w in res.warnings:
                st.warning(f"{label}: {w}")
            if res.ok:
                saver(res.df)
                st.success(f"{label}: {len(res.df)} rows saved")
            else:
                for err in res.errors:
                    st.error(f"{label}: {err}")

    with st.expander("Admin"):
        st.caption(f"Database: {db.database_label()}")
        if st.button("Load October 2026 sample"):
            inv_s, lt_s, ht_s = load_sample()
            db.save_inverter(inv_s); db.save_lt(lt_s); db.save_ht(ht_s)
            st.success("Sample data loaded")

    st.divider()
    months = db.available_months()
    if not months:
        st.info("No data yet. Upload the three sheets or load the sample.")
        st.stop()
    month = st.selectbox("Report month", months,
                         format_func=lambda m: pd.Timestamp(m + "-01").strftime("%B %Y"))


inv, lt, ht = db.load_month(month)
if inv.empty:
    st.warning("No inverter data for this month.")
    st.stop()

first, last = inv["date"].min(), inv["date"].max()

with st.sidebar:
    compare_through = st.date_input(
        "Compare LT vs inverters up to", value=(last - pd.Timedelta(days=1)).date(),
        min_value=first.date(), max_value=last.date(),
        help="Exclude a last day whose inverter data was exported before sunset.",
    )

kpis = headline_kpis(inv, lt, ht, pd.Timestamp(compare_through))
totals = inverter_totals(inv)
low = totals.loc[totals["vs_others"] < LOW_INVERTER_RATIO, "inverter"].tolist()
period_txt = f"{first:%d}–{last:%d %B %Y}"


# ---------------- Header ----------------
st.title(f"{PLANT_NAME}: {first:%B %Y} generation and grid report")
st.caption(f"{PLANT_CAPACITY_KWP} kWp rooftop solar · Inverters PV1–PV9 · LT generation meter (MF 300) · "
           f"HT bi-directional meter (MF 6000) · {period_txt}")
st.markdown(takeaway(kpis, inv))
if GRAFANA_URL:
    st.link_button("Open live monitoring in Grafana", GRAFANA_URL)


# ---------------- KPI cards ----------------
cards = [
    ("Inverter generation", f"{kpis['inverter_kwh']:,.0f}", "kWh · inverter sheet"),
    ("LT meter generation", f"{kpis['lt_kwh']:,.0f}", "kWh · LT meter"),
    ("HT grid import", f"{kpis['ht_import_kwh']:,.0f}", "kWh from BESCOM"),
    ("HT grid export", f"{kpis['ht_export_kwh']:,.0f}", "kWh to BESCOM"),
    ("Site consumption", f"{kpis['site_load_kwh']:,.0f}", "kWh · HT net import + LT solar"),
    ("Solar share of load", f"{kpis['solar_share']:.0%}", "LT solar ÷ site consumption"),
]
for col, (label, value, note) in zip(st.columns(6), cards):
    with col.container(border=True):
        st.metric(label, value)
        st.caption(note)


# ---------------- Daily generation ----------------
with st.container(border=True):
    st.subheader("Daily generation with weather")
    st.caption("Bars: inverter totals (red = outage day, below 20% of the month's median). "
               "Dots: LT meter readings for single-day periods; combined LT periods have no dot.")
    st.plotly_chart(charts.daily_generation(inv, lt), use_container_width=True)


# ---------------- Inverter-wise table ----------------
def style_inverters(df: pd.DataFrame):
    flags = flagged_cells(inv)
    outage = outage_mask(inv)
    vmax = inv[INVERTER_IDS].to_numpy().max() or 1

    def cell_styles(_):
        styles = pd.DataFrame("", index=df.index, columns=df.columns)
        for p in INVERTER_IDS:
            for i in df.index:
                v = df.at[i, p]
                if flags.at[i, p]:
                    styles.at[i, p] = "background-color: rgba(228,87,86,0.25); color: #c0392b; font-weight: 600"
                else:
                    styles.at[i, p] = f"background-color: rgba(76,120,168,{0.06 + 0.34 * v / vmax:.2f})"
        styles.loc[outage.values, ["Date", "Day", "Total"]] = "color: #c0392b"
        styles["Total"] = styles["Total"].map(lambda s: (s + "; " if s else "") + "font-weight: 600")
        return styles

    return df.style.apply(cell_styles, axis=None).format({p: "{:,.2f}" for p in [*INVERTER_IDS, "Total"]})


with st.container(border=True):
    st.subheader("Inverter-wise readings (kWh)")
    st.caption("PV1–PV3 RR Auditorium · PV4–PV6 RR College · PV7 Hospital Roof 2 · PV8 Hospital Roof 1 · "
               "PV9 Hospital Annex Roof 3. Red cell = below half of that day's median inverter.")
    table = inv[["date", "day", *INVERTER_IDS, "Total"]].copy()
    table["date"] = table["date"].dt.strftime("%d %b")
    table = table.rename(columns={"date": "Date", "day": "Day"})
    total_row = pd.DataFrame([{"Date": "Total", "Day": "", **inv[[*INVERTER_IDS, "Total"]].sum().to_dict()}])
    st.dataframe(style_inverters(table), hide_index=True, use_container_width=True)
    st.dataframe(total_row.style.format({p: "{:,.2f}" for p in [*INVERTER_IDS, "Total"]}),
                 hide_index=True, use_container_width=True)


# ---------------- Inverter totals + grid exchange ----------------
c1, c2 = st.columns(2)
with c1.container(border=True):
    st.subheader("Month to date by inverter")
    st.caption("Chronically low inverters shown in red.")
    st.plotly_chart(charts.inverter_totals_bar(totals, low), use_container_width=True)
with c2.container(border=True):
    st.subheader("Grid exchange at the HT meter")
    st.caption("Top: daily import from BESCOM. Bottom: export on its own scale.")
    st.plotly_chart(charts.grid_exchange(ht), use_container_width=True)


# ---------------- Consolidated statement ----------------
daily = daily_statement(inv, lt, ht)
with st.container(border=True):
    st.subheader("Consolidated daily statement")
    show = daily.copy()
    show["date"] = show["date"].dt.strftime("%d %b")
    show["weather"] = show["weather"].map(lambda w: f"{charts.weather_icon(w)} {w}")
    show = show.rename(columns={
        "date": "Date", "day": "Day", "weather": "Weather", "temp_c": "Temp °C", "wind_ms": "Wind m/s",
        "inverter_kwh": "Inverter kWh", "lt_kwh": "LT meter kWh", "lt_period": "LT period",
        "ht_import_kwh": "HT import kWh", "ht_export_kwh": "HT export kWh", "ht_net_kwh": "HT net import kWh",
    })
    num = ["Inverter kWh", "LT meter kWh", "HT import kWh", "HT export kWh", "HT net import kWh"]
    st.dataframe(
        show.style.format({**{c: "{:,.0f}" for c in num}, "Temp °C": "{:.1f}", "Wind m/s": "{:.1f}"}, na_rep="—"),
        hide_index=True, use_container_width=True,
    )
    st.caption(f"Totals: inverter {kpis['inverter_kwh']:,.0f} · LT {kpis['lt_kwh']:,.0f} · "
               f"import {kpis['ht_import_kwh']:,.0f} · export {kpis['ht_export_kwh']:,.0f} · "
               f"net {kpis['ht_net_kwh']:,.0f} kWh")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        daily.to_excel(xw, sheet_name="Daily statement", index=False)
        inv.to_excel(xw, sheet_name="Inverters", index=False)
        lt.to_excel(xw, sheet_name="LT meter", index=False)
        ht.to_excel(xw, sheet_name="HT meter", index=False)
        totals.drop(columns="zero_days").to_excel(xw, sheet_name="Inverter totals", index=False)
    st.download_button("Download report data (Excel)", buf.getvalue(),
                       file_name=f"srtpv_report_{month}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------- Observations ----------------
with st.container(border=True):
    st.subheader("Observations and follow-up")
    for title, detail in build_observations(inv, lt, ht, kpis):
        st.markdown(f"- **{title}** {detail}")

    note = st.text_area("Engineer's notes for this month", value=db.load_note(month), height=120)
    if st.button("Save notes"):
        db.save_note(month, note)
        st.success("Notes saved")
