"""SRTPV plant generation & grid report.

Run:  streamlit run app.py
Admins import data and manage access; customers view and filter reports.
"""
from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from core import auth, charts, db
from core.calculations import (
    ac_loss_totals,
    ac_losses,
    customer_averages,
    daily_statement,
    grid_summary,
    flagged_cells,
    headline_kpis,
    inverter_totals,
    outage_mask,
)
from core.config import (
    INVERTER_IDS,
    INVERTERS,
    LOW_INVERTER_RATIO,
    PLANT_CAPACITY_KWP,
    PF_LIMIT,
    PLANT_NAME,
    PRODUCTIVITY_BANDS,
    WEATHER_GROUPS,
)
from core.filters import apply_filters, enrich, productivity_summary
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


# =====================================================================
# Login
# =====================================================================
def login_page() -> None:
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.title("☀️ " + PLANT_NAME)
        st.caption(f"{PLANT_CAPACITY_KWP} kWp rooftop solar · generation and grid reports")
        with st.form("login"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log in", type="primary", use_container_width=True)
        if submitted:
            try:
                user = auth.sign_in(email, password)
            except auth.AuthError as e:
                st.error(str(e))
                return
            role = auth.role_for(user["email"])
            if role is None:
                st.error("Your login works, but you haven't been given access yet. Please contact the administrator.")
                return
            st.session_state["user"] = {"email": user["email"], "role": role}
            st.rerun()
        st.caption("Forgot your password? Ask the administrator to reset it.")


if "user" not in st.session_state:
    login_page()
    st.stop()

USER = st.session_state["user"]
ROLE = USER["role"]
IS_ADMIN = ROLE == "admin"
IS_SITE = ROLE == "site_manager"
IS_CUSTOMER = ROLE == "customer"
CAN_IMPORT = IS_ADMIN or IS_SITE


# =====================================================================
# Admin pages
# =====================================================================
def import_page() -> None:
    st.subheader("Import monthly data")
    st.caption("Upload any of the three sheets (.xlsx or .csv). Each file is checked first; "
               "nothing is saved if a check fails. Re-importing a date replaces it.")
    c1, c2, c3 = st.columns(3)
    f_inv = c1.file_uploader("Inverter daily readings (PV1–PV9)", type=["xlsx", "csv"])
    f_lt = c2.file_uploader("LT generation meter", type=["xlsx", "csv"])
    f_ht = c3.file_uploader("HT import / export meter", type=["xlsx", "csv"])

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

    if not IS_ADMIN:
        return
    with st.expander("Tools"):
        st.caption(f"Database: {db.database_label()}")
        if st.button("Load October 2026 sample"):
            inv_s, lt_s, ht_s = load_sample()
            db.save_inverter(inv_s); db.save_lt(lt_s); db.save_ht(ht_s)
            st.success("Sample data loaded")


def users_page() -> None:
    st.subheader("User access")
    st.markdown(
        "**Step 1:** create the person's login in Supabase → *Authentication → Users → Add user* "
        "(tick *Auto confirm user*).  \n"
        "**Step 2:** give that email a role here.  \n"
        "• **Backend user**: full report incl. AC losses, EB and power factor; imports data; manages users  \n"
        "• **Site manager**: meter and inverter readings with summary figures; imports data  \n"
        "• **Customer**: averages only (average of all inverters, average daily figures)"
    )
    users = db.list_users()
    boot = sorted(auth.admin_emails())
    if boot:
        st.caption(f"Always admin (from the ADMIN_EMAILS secret): {', '.join(boot)}")
    st.dataframe(users.assign(role=users["role"].map(lambda r: auth.ROLE_LABELS.get(r, r))),
                 hide_index=True, use_container_width=True)

    c1, c2 = st.columns(2)
    with c1.form("add_user", clear_on_submit=True):
        st.markdown("**Add or change a user**")
        email = st.text_input("Email (same as in Supabase)")
        name = st.text_input("Name")
        role = st.selectbox("Role", auth.ROLES, index=2, format_func=lambda r: auth.ROLE_LABELS[r])
        if st.form_submit_button("Save access", type="primary"):
            if "@" not in email:
                st.error("Enter a valid email")
            else:
                db.save_user(email, name, role)
                st.success(f"{email} is now: {auth.ROLE_LABELS[role]}")
                st.rerun()
    with c2.form("remove_user"):
        st.markdown("**Remove access**")
        target = st.selectbox("User", users["email"].tolist() or ["—"])
        if st.form_submit_button("Remove") and target != "—":
            if target == USER["email"]:
                st.error("You can't remove your own access.")
            else:
                db.remove_user(target)
                st.success(f"Removed {target}")
                st.rerun()
        st.caption("This removes access to this app. The Supabase login stays until you delete it there.")


# =====================================================================
# Sidebar: user + filters
# =====================================================================
with st.sidebar:
    st.markdown(f"**{USER['email']}**  \n{auth.ROLE_LABELS.get(ROLE, ROLE)}")
    if st.button("Log out"):
        st.session_state.clear()
        st.rerun()
    st.divider()

bounds = db.data_bounds()
if bounds is None:
    if IS_ADMIN:
        st.info("No data yet. Import the three sheets, or load the October sample under Tools.")
        tab_imp, tab_users = st.tabs(["Import data", "User access"])
        with tab_imp:
            import_page()
        with tab_users:
            users_page()
    elif IS_SITE:
        st.info("No data yet. Import the three monthly sheets.")
        import_page()
    else:
        st.info("No data has been published yet.")
    st.stop()

with st.sidebar:
    st.header("Filters")
    mode = st.radio("Period", ["Month", "Date range"], horizontal=True, key="f_mode")
    if mode == "Month":
        months = db.available_months()
        month = st.selectbox("Month", months, format_func=lambda m: pd.Timestamp(m + "-01").strftime("%B %Y"), key="f_month")
        inv_all, lt_all, ht_all = db.load_month(month)
        start = end = None
    else:
        month = None
        picked = st.date_input("From – to", value=(bounds[0], bounds[1]),
                               min_value=bounds[0], max_value=bounds[1], format="DD-MM-YYYY", key="f_range")
        if not isinstance(picked, tuple) or len(picked) != 2:
            st.info("Pick an end date.")
            st.stop()
        start, end = pd.Timestamp(picked[0]), pd.Timestamp(picked[1])
        inv_all, lt_all, ht_all = db.load_range(picked[0], picked[1])

    if inv_all.empty:
        st.warning("No data in this period.")
        st.stop()
    inv_all = enrich(inv_all)

    weather_opts = [w for w in WEATHER_GROUPS if w in set(inv_all["weather_group"])]
    weather = st.multiselect("Weather", weather_opts, default=weather_opts, key="f_weather")
    band_names = [b[0] for b in PRODUCTIVITY_BANDS]
    band_help = ", ".join(f"{n} {lo:g}–{hi:g}" if hi != float("inf") else f"{n} ≥ {lo:g}"
                          for n, lo, hi in PRODUCTIVITY_BANDS)
    productivity = st.multiselect("Productivity (kWh per kWp per day)", band_names, default=band_names,
                                  help=band_help, key="f_prod")
    day_types = st.multiselect("Days", ["Weekday", "Weekend"], default=["Weekday", "Weekend"], key="f_days")
    if IS_CUSTOMER:
        shown_inverters = INVERTER_IDS
    else:
        shown_inverters = st.multiselect("Inverters in table and chart", INVERTER_IDS, default=INVERTER_IDS,
                                         format_func=lambda p: f"{p} · {INVERTERS[p]}", key="f_inv")
    if st.button("Reset filters"):
        for k in [k for k in st.session_state if str(k).startswith("f_")]:
            del st.session_state[k]
        st.rerun()

inv, lt, ht = apply_filters(inv_all, lt_all, ht_all, start, end, weather, productivity, day_types)
filtered = len(inv) < len(inv_all)

if inv.empty:
    st.warning("No days match these filters. Widen the weather, productivity or day filters.")
    st.stop()
if not shown_inverters:
    shown_inverters = INVERTER_IDS

first, last = inv["date"].min(), inv["date"].max()
with st.sidebar.expander("Advanced", expanded=False):
    default_cmp = max(first, last - pd.Timedelta(days=1)).date()
    compare_through = st.date_input(
        "Compare LT vs inverters up to", value=default_cmp, min_value=first.date(), max_value=last.date(),
        help="Exclude a last day whose inverter data was exported before sunset.", format="DD-MM-YYYY",
    )


# =====================================================================
# Report
# =====================================================================
def style_inverters(df: pd.DataFrame, source: pd.DataFrame):
    flags = flagged_cells(source)
    outage = outage_mask(source)
    cols = [p for p in INVERTER_IDS if p in df.columns]
    vmax = source[INVERTER_IDS].to_numpy().max() or 1

    def cell_styles(_):
        styles = pd.DataFrame("", index=df.index, columns=df.columns)
        for p in cols:
            for i in df.index:
                v = df.at[i, p]
                if flags.at[i, p]:
                    styles.at[i, p] = "background-color: rgba(228,87,86,0.25); color: #c0392b; font-weight: 600"
                else:
                    styles.at[i, p] = f"background-color: rgba(76,120,168,{0.06 + 0.34 * v / vmax:.2f})"
        styles.loc[outage.values, ["Date", "Day", "Total"]] = "color: #c0392b"
        styles["Total"] = styles["Total"].map(lambda s: (s + "; " if s else "") + "font-weight: 600")
        return styles

    return df.style.apply(cell_styles, axis=None).format({p: "{:,.2f}" for p in [*cols, "Total"]})


# ---------------------------------------------------------------------
# Report sections (each role gets a different combination)
# ---------------------------------------------------------------------
KPIS = headline_kpis(inv, lt, ht, pd.Timestamp(compare_through))
GRID = grid_summary(ht, lt)


def header(subtitle: str = "") -> None:
    if month:
        heading = f"{pd.Timestamp(month + '-01'):%B %Y} generation and grid report"
    else:
        heading = f"Generation and grid report, {first:%d %b %Y} – {last:%d %b %Y}"
    st.title(f"{PLANT_NAME}: {heading}")
    st.caption(f"{PLANT_CAPACITY_KWP} kWp rooftop solar · {first:%d %b} – {last:%d %b %Y}"
               + (f" · {subtitle}" if subtitle else ""))
    if filtered:
        st.info(f"Filters on: showing **{len(inv)} of {len(inv_all)} days**. Figures cover only these days.")


def cards(items: list[tuple[str, str, str]]) -> None:
    for col, (label, value, note) in zip(st.columns(len(items)), items):
        with col.container(border=True):
            st.metric(label, value)
            st.caption(note)


def kpi_cards() -> None:
    cards([
        ("Inverter generation", f"{KPIS['inverter_kwh']:,.0f}", "kWh · inverter sheet"),
        ("LT meter generation", f"{KPIS['lt_kwh']:,.0f}", "kWh · LT meter"),
        ("EB import", f"{KPIS['ht_import_kwh']:,.0f}", "kWh from BESCOM (HT meter)"),
        ("Export to EB", f"{KPIS['ht_export_kwh']:,.0f}", "kWh to BESCOM (HT meter)"),
        ("Site consumption", f"{KPIS['site_load_kwh']:,.0f}", "kWh · net EB + LT solar"),
        ("Solar share of load", f"{KPIS['solar_share']:.0%}", "LT solar ÷ site consumption"),
    ])


def pf_text() -> str:
    if GRID["pf_avg"] is None:
        return "not recorded"
    return f"{GRID['pf_avg']:.3f} avg · {GRID['pf_min']:.3f} min"


# ---------------- backend-only analysis ----------------
def ac_loss_section() -> None:
    losses = ac_losses(inv, lt)
    with st.container(border=True):
        st.subheader("AC loss: inverters → LT panel")
        st.caption("Energy lost on the AC run between the inverter outputs and the LT meter at the LT panel. "
                   "Loss = inverter kWh − LT meter kWh, compared over the same days for each LT reading period.")
        if losses.empty:
            st.info("No LT periods with complete inverter data in this view.")
            return
        t = ac_loss_totals(losses)
        cards([
            ("AC loss (clean periods)", f"{t['clean_loss_kwh']:,.0f} kWh", f"{t['clean_loss_pct']:.1%} of inverter output"),
            ("Inverter output", f"{t['clean_inverter_kwh']:,.0f} kWh", f"{t['clean_periods']} LT periods"),
            ("Reached LT meter", f"{t['clean_lt_kwh']:,.0f} kWh", "same periods"),
            ("Data-gap periods", f"{t['gap_periods']}", "LT higher than inverters; left out of the loss"),
        ])
        st.plotly_chart(charts.ac_loss_chart(losses), use_container_width=True)
        show = losses.assign(**{"from": losses["from_date"].dt.strftime("%d %b"),
                                "to": losses["to_date"].dt.strftime("%d %b")})
        show = show[["period", "days", "inverter_kwh", "lt_kwh", "loss_kwh", "loss_pct", "status"]].rename(columns={
            "period": "LT period", "days": "Days", "inverter_kwh": "Inverter kWh", "lt_kwh": "LT meter kWh",
            "loss_kwh": "Loss kWh", "loss_pct": "Loss %", "status": "Status"})
        st.dataframe(show.style.format({"Inverter kWh": "{:,.0f}", "LT meter kWh": "{:,.0f}",
                                        "Loss kWh": "{:,.0f}", "Loss %": "{:.1%}"}),
                     hide_index=True, use_container_width=True)
        st.caption(f"Over all periods including data gaps: {t['loss_kwh']:,.0f} kWh ({t['loss_pct']:.1%}). "
                   "Expected AC loss is set in core/config.py (AC_LOSS_EXPECTED).")


def eb_pf_section() -> None:
    with st.container(border=True):
        st.subheader("Energy balance with EB and power factor")
        cards([
            ("Generated (LT meter)", f"{GRID['generated_kwh']:,.0f}", "kWh solar delivered to the LT panel"),
            ("EB import", f"{GRID['eb_import_kwh']:,.0f}", "kWh bought from BESCOM"),
            ("Export to EB", f"{GRID['export_kwh']:,.0f}", f"kWh · {GRID['export_share_of_generation']:.1%} of generation"),
            ("Net EB", f"{GRID['net_eb_kwh']:,.0f}", "kWh import − export"),
            ("Power factor", pf_text(), f"limit {PF_LIMIT:.2f} · {len(GRID['pf_low_days'])} day(s) below"),
        ])
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Daily EB import and export**")
            if ht.empty:
                st.info("No HT meter data for these days.")
            else:
                st.plotly_chart(charts.grid_exchange(ht), use_container_width=True)
        with c2:
            st.markdown("**Power factor at the HT meter**")
            if GRID["pf_days_recorded"]:
                st.plotly_chart(charts.power_factor_chart(ht), use_container_width=True)
                if GRID["pf_low_days"]:
                    st.warning(f"PF below {PF_LIMIT:.2f} on {', '.join(GRID['pf_low_days'])}. "
                               "Check capacitor bank / APFC panel; low PF can attract a tariff penalty.")
            else:
                st.info("No power factor in the HT sheets yet. Add a power_factor column (e.g. 0.97) "
                        "to the HT meter Excel and re-import.")


def productivity_section() -> None:
    with st.container(border=True):
        st.subheader("Productivity")
        best = inv.loc[inv["yield_kwh_kwp"].idxmax()]
        worst = inv.loc[inv["yield_kwh_kwp"].idxmin()]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Average yield", f"{inv['yield_kwh_kwp'].mean():.2f}", help="kWh per kWp per day")
        m2.metric("Best day", f"{best['yield_kwh_kwp']:.2f}", f"{best['date']:%d %b} · {best['weather_group']}",
                  delta_color="off")
        m3.metric("Worst day", f"{worst['yield_kwh_kwp']:.2f}", f"{worst['date']:%d %b} · {worst['weather_group']}",
                  delta_color="off")
        m4.metric("Days in view", len(inv))
        p1, p2 = st.columns(2)
        p1.dataframe(productivity_summary(inv).style.format({"kwh": "{:,.0f}"}),
                     hide_index=True, use_container_width=True)
        p2.dataframe(weather_table().style.format({"avg kWh/kWp": "{:.2f}", "kwh": "{:,.0f}"}),
                     hide_index=True, use_container_width=True)


def weather_table() -> pd.DataFrame:
    t = (inv.groupby("weather_group")
         .agg(days=("date", "count"), avg_yield=("yield_kwh_kwp", "mean"), kwh=("Total", "sum"))
         .reindex([w for w in WEATHER_GROUPS if w in set(inv["weather_group"])]).reset_index())
    return t.rename(columns={"weather_group": "weather", "avg_yield": "avg kWh/kWp"})


def daily_chart() -> None:
    with st.container(border=True):
        st.subheader("Daily generation with weather")
        st.caption("Bars: inverter totals (red = outage day). Dots: LT meter readings for single-day periods.")
        st.plotly_chart(charts.daily_generation(inv, lt), use_container_width=True)


def inverter_table() -> None:
    with st.container(border=True):
        st.subheader("Inverter-wise readings (kWh)")
        st.caption("Red cell = below half of that day's median inverter. Total always includes all nine inverters.")
        table = inv[["date", "day", *shown_inverters, "Total"]].copy()
        table["date"] = table["date"].dt.strftime("%d %b")
        table = table.rename(columns={"date": "Date", "day": "Day"})
        st.dataframe(style_inverters(table, inv), hide_index=True, use_container_width=True)
        total_row = pd.DataFrame([{"Date": "Total", "Day": "", **inv[[*shown_inverters, "Total"]].sum().to_dict()}])
        st.dataframe(total_row.style.format({p: "{:,.2f}" for p in [*shown_inverters, "Total"]}),
                     hide_index=True, use_container_width=True)


def inverter_ranking() -> None:
    totals = inverter_totals(inv)
    low = totals.loc[totals["vs_others"] < LOW_INVERTER_RATIO, "inverter"].tolist()
    with st.container(border=True):
        st.subheader("Total by inverter")
        st.caption("Chronically low inverters shown in red.")
        st.plotly_chart(charts.inverter_totals_bar(totals[totals["inverter"].isin(shown_inverters)], low),
                        use_container_width=True)


def meter_readings() -> None:
    with st.container(border=True):
        st.subheader("Meter readings")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**LT generation meter** (kWh = units × 300)")
            t = lt.assign(**{"from": lt["from_date"].dt.strftime("%d %b"), "to": lt["to_date"].dt.strftime("%d %b")})
            st.dataframe(t[["period", "opening", "closing", "units", "kwh", "note"]]
                         .style.format({"opening": "{:.1f}", "closing": "{:.1f}", "units": "{:.1f}", "kwh": "{:,.0f}"}),
                         hide_index=True, use_container_width=True)
        with c2:
            st.markdown("**HT meter: EB import / export and power factor**")
            h = ht.assign(date=ht["date"].dt.strftime("%d %b"))
            st.dataframe(h[["date", "day", "import_kwh", "export_kwh", "net_import_kwh", "power_factor", "note"]]
                         .style.format({"import_kwh": "{:,.0f}", "export_kwh": "{:,.0f}",
                                        "net_import_kwh": "{:,.0f}", "power_factor": "{:.3f}"}, na_rep="—"),
                         hide_index=True, use_container_width=True)


def build_daily() -> pd.DataFrame:
    daily = daily_statement(inv, lt, ht)
    daily.insert(3, "weather_group", inv["weather_group"].values)
    daily.insert(7, "yield_kwh_kwp", inv["yield_kwh_kwp"].values)
    daily.insert(8, "productivity", inv["productivity"].values)
    pf = ht.set_index("date")["power_factor"] if len(ht) else pd.Series(dtype=float)
    daily["power_factor"] = daily["date"].map(pf)
    return daily


def statement_and_download(full: bool) -> None:
    daily = build_daily()
    with st.container(border=True):
        st.subheader("Consolidated daily statement")
        show = daily.copy()
        show["date"] = show["date"].dt.strftime("%d %b %Y")
        show["weather"] = show["weather"].map(lambda w: f"{charts.weather_icon(w)} {w}")
        show = show.rename(columns={
            "date": "Date", "day": "Day", "weather": "Weather", "weather_group": "Weather group",
            "temp_c": "Temp °C", "wind_ms": "Wind m/s", "inverter_kwh": "Inverter kWh",
            "yield_kwh_kwp": "kWh/kWp", "productivity": "Productivity",
            "lt_kwh": "LT meter kWh", "lt_period": "LT period", "ht_import_kwh": "EB import kWh",
            "ht_export_kwh": "Export kWh", "ht_net_kwh": "Net EB kWh", "power_factor": "PF",
        })
        num = ["Inverter kWh", "LT meter kWh", "EB import kWh", "Export kWh", "Net EB kWh"]
        st.dataframe(
            show.style.format({**{c: "{:,.0f}" for c in num}, "Temp °C": "{:.1f}", "Wind m/s": "{:.1f}",
                               "kWh/kWp": "{:.2f}", "PF": "{:.3f}"}, na_rep="—"),
            hide_index=True, use_container_width=True,
        )
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            daily.to_excel(xw, sheet_name="Daily statement", index=False)
            inv.drop(columns=["is_outage"]).to_excel(xw, sheet_name="Inverters", index=False)
            lt.to_excel(xw, sheet_name="LT meter", index=False)
            ht.to_excel(xw, sheet_name="HT meter", index=False)
            if full:
                ac_losses(inv, lt).to_excel(xw, sheet_name="AC losses", index=False)
                inverter_totals(inv).drop(columns="zero_days").to_excel(xw, sheet_name="Inverter totals", index=False)
        download(buf, "report")


def download(buf: io.BytesIO, kind: str) -> None:
    fname = f"srtpv_{kind}_{month or f'{first:%Y%m%d}-{last:%Y%m%d}'}{'_filtered' if filtered else ''}.xlsx"
    st.download_button("Download this view (Excel)", buf.getvalue(), file_name=fname,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def observations_and_notes(editable: bool) -> None:
    with st.container(border=True):
        st.subheader("Observations and follow-up")
        for title, detail in build_observations(inv, lt, ht, KPIS):
            st.markdown(f"- **{title}** {detail}")
        notes_box(editable)


def notes_box(editable: bool) -> None:
    if not month:
        return
    note = db.load_note(month)
    if editable:
        new = st.text_area("Engineer's notes for this month", value=note, height=120)
        if st.button("Save notes"):
            db.save_note(month, new)
            st.success("Notes saved")
    elif note:
        st.markdown("**Engineer's notes**")
        st.info(note)


# ---------------------------------------------------------------------
# Role views
# ---------------------------------------------------------------------
def backend_report() -> None:
    header("Backend view: full report")
    st.markdown(takeaway(KPIS, inv))
    if GRAFANA_URL:
        st.link_button("Open live monitoring in Grafana", GRAFANA_URL)
    kpi_cards()
    ac_loss_section()
    eb_pf_section()
    productivity_section()
    daily_chart()
    inverter_table()
    inverter_ranking()
    meter_readings()
    statement_and_download(full=True)
    observations_and_notes(editable=True)


def site_manager_report() -> None:
    header("Site view: readings and summary")
    cards([
        ("Inverter generation", f"{KPIS['inverter_kwh']:,.0f}", "kWh"),
        ("LT meter generation", f"{KPIS['lt_kwh']:,.0f}", "kWh"),
        ("EB import", f"{KPIS['ht_import_kwh']:,.0f}", "kWh"),
        ("Export to EB", f"{KPIS['ht_export_kwh']:,.0f}", "kWh"),
        ("Power factor", pf_text(), f"limit {PF_LIMIT:.2f}"),
    ])
    daily_chart()
    inverter_table()
    meter_readings()
    statement_and_download(full=False)
    with st.container(border=True):
        notes_box(editable=False)


def customer_report() -> None:
    header("Customer summary")
    a = customer_averages(inv, lt, ht)
    cards([
        ("Average daily generation", f"{a['avg_daily_generation']:,.0f} kWh", "whole plant, per day"),
        ("Average per inverter", f"{a['avg_per_inverter_daily']:,.0f} kWh", "average of all 9 inverters, per day"),
        ("Average yield", f"{a['avg_yield']:.2f}", "kWh per kWp per day"),
        ("Solar share of load", f"{a['solar_share']:.0%}", "share of site energy from solar"),
    ])
    with st.container(border=True):
        st.subheader("Daily average output per inverter")
        st.caption("Each bar is the plant's output that day divided by 9 inverters; the dotted line is the period average.")
        st.plotly_chart(charts.customer_daily(inv), use_container_width=True)
    with st.container(border=True):
        st.subheader("Average output by weather")
        wt = weather_table()
        wt["avg kWh per day"] = wt["kwh"] / wt["days"]
        wt["avg kWh per inverter per day"] = wt["avg kWh per day"] / len(INVERTER_IDS)
        wt = wt[["weather", "days", "avg kWh per day", "avg kWh per inverter per day", "avg kWh/kWp"]]
        st.dataframe(wt.style.format({"avg kWh per day": "{:,.0f}", "avg kWh per inverter per day": "{:,.0f}",
                                      "avg kWh/kWp": "{:.2f}"}),
                     hide_index=True, use_container_width=True)
        summary = pd.DataFrame([{
            "From": f"{first:%d %b %Y}", "To": f"{last:%d %b %Y}", "Days": a["days"],
            "Total generation kWh": round(a["total_generation"]),
            "Average daily generation kWh": round(a["avg_daily_generation"]),
            "Average per inverter per day kWh": round(a["avg_per_inverter_daily"]),
            "Average yield kWh/kWp/day": round(a["avg_yield"], 2),
            "Solar share of load": f"{a['solar_share']:.0%}",
        }])
        daily_avg = pd.DataFrame({
            "date": inv["date"].dt.date, "weather": inv["weather"],
            "plant kWh": inv["Total"].round(0),
            "average per inverter kWh": (inv["Total"] / len(INVERTER_IDS)).round(1),
            "kWh/kWp": inv["yield_kwh_kwp"].round(2),
        })
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            summary.to_excel(xw, sheet_name="Summary", index=False)
            daily_avg.to_excel(xw, sheet_name="Daily averages", index=False)
            wt.to_excel(xw, sheet_name="By weather", index=False)
        download(buf, "summary")


if IS_ADMIN:
    tab_report, tab_import, tab_users = st.tabs(["Report", "Import data", "User access"])
    with tab_report:
        backend_report()
    with tab_import:
        import_page()
    with tab_users:
        users_page()
elif IS_SITE:
    tab_report, tab_import = st.tabs(["Report", "Import data"])
    with tab_report:
        site_manager_report()
    with tab_import:
        import_page()
else:
    customer_report()
