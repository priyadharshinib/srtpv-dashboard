"""SRTPV plant generation & grid report.

Run:  streamlit run app.py
Admins import data and manage access; customers view and filter reports.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
from html import escape
from pathlib import Path

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
    pv_comparison,
)
from core.config import (
    INVERTER_IDS,
    INVERTERS,
    LOW_INVERTER_RATIO,
    PLANT_CAPACITY_KWP,
    BRAND,
    CONFIDENTIAL,
    LT_METER_MF,
    PF_LIMIT,
    PLANT_NAME,
    PLANT_SHORT,
    PLANT_SITE,
    PRODUCTIVITY_BANDS,
    WEATHER_GROUPS,
)
from core.filters import apply_filters, enrich, productivity_summary
from core.importer import import_ht_meter, import_inverter, import_lt_meter
from core.observations import glance_headline, inverter_issues
from core.report import bill_totals_fallback, build_period, daily_headline, day_ranges, fill_bill_lt, followups, month_list, short_weather, very_low_mask
from core.settings import get_setting
from core.views import PROJECTED, apply_view, views_for
from core.projection import chronic_inverters, faulty_cells, project, share_for
from core.billing import BILL_LT_DISCLAIMER, demand_headline, demand_pct, demand_story, ht_import_breakdown, inr
from core.batches import ADD_NEW, ALL_MONTHS, REPLACE, REPLACE_MONTH, coverage_text, prepare_batch, split_by_month
from core.config import CONTRACT_DEMAND_KVA, DEFAULT_EXPORT_NOTE, PROJECTION_SHARE

st.set_page_config(page_title=PLANT_NAME, page_icon="☀️", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@400;600;700&display=swap');
.ae-logo-top{display:flex;justify-content:center;align-items:center;gap:14px;margin:0 0 14px;flex-wrap:nowrap}
.ae-logo-top img{max-width:62vw;height:auto}
.ae-logo-top img.mark{height:54px;width:auto}
.ae-logo-top img.word{height:44px;width:auto}
.ae-title{font-family:'Source Serif 4',Cambria,Georgia,serif;font-weight:700;color:#1C2B3A;
          text-align:center;font-size:2.4rem;line-height:1.15;margin:0}
.ae-subtitle{font-family:'Source Serif 4',Cambria,Georgia,serif;text-align:center;color:#B57D0E;font-size:1.5rem}
.ae-small{text-align:center;color:#5B6B7B;margin-top:4px}
@media (max-width:640px){
  .ae-logo-top{gap:8px;margin-bottom:10px}
  .ae-logo-top img.mark{height:30px}
  .ae-logo-top img.word{height:24px}
  .ae-title{font-size:1.6rem}
  .ae-subtitle{font-size:1.1rem}
  .block-container{padding-left:.8rem !important;padding-right:.8rem !important;padding-top:2.2rem !important}
}
</style>""", unsafe_allow_html=True)


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


def projection_shares() -> dict:
    """Shares saved in the app per month; any other month uses PROJECTION_SHARE."""
    try:
        return db.get_projection_shares()
    except Exception:
        return {}


# =====================================================================
# Login
# =====================================================================
def _logo_html() -> str:
    folder = Path(__file__).parent / "assets"
    imgs = [base64.b64encode((folder / f).read_bytes()).decode() for f in ("logo_mark.png", "logo_word.png")
            if (folder / f).exists()]
    if len(imgs) < 2:
        return ""
    return (f'<div class="ae-logo-top"><img class="mark" src="data:image/png;base64,{imgs[0]}">'
            f'<img class="word" src="data:image/png;base64,{imgs[1]}"></div>')


def login_page() -> None:
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(_logo_html() + f'<div class="ae-title">{escape(PLANT_NAME)}</div>'
                    f'<div class="ae-subtitle">{escape(PLANT_SITE)}</div>'
                    f'<div class="ae-small">{PLANT_CAPACITY_KWP} kWp rooftop solar · generation and grid reports</div>',
                    unsafe_allow_html=True)
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
def bill_form() -> None:
    st.divider()
    st.subheader("Monthly HT bill")
    st.caption("Enter the figures from the BESCOM HT bill once per month. Saving again replaces that month. "
               "With the energy totals filled in, the dashboard uses the bill as the month's LT and HT figures.")
    months = db.available_months()
    if not months:
        st.info("Import the month's readings first, then enter its bill.")
        return
    month_sel = st.selectbox("Bill month", months, key="bill_month",
                             format_func=lambda m: pd.Timestamp(m + "-01").strftime("%B %Y"))
    b = db.load_bill(month_sel) or {}
    md_at = b.get("md_at") or dt.datetime.fromisoformat(month_sel + "-01 10:00")
    with st.form(f"bill_{month_sel}"):
        c1, c2, c3 = st.columns(3)
        net = c1.number_input("Net payable (₹) (optional)", min_value=0.0, step=1.0, value=float(b.get("net_payable") or 0))
        pen = c2.number_input("PF penalty (₹)", min_value=0.0, step=1.0, value=float(b.get("pf_penalty") or 0))
        pf = c3.number_input("Average power factor (optional, 0 = not shown)", min_value=0.0, max_value=1.0, step=0.001,
                             format="%.3f", value=float(b.get("avg_pf") or 0))
        c4, c5 = st.columns(2)
        adj_label = c4.text_input("Other charge on the bill (name)", value=b.get("adjustment_label") or "",
                                  placeholder="e.g. FY25 true-up")
        adj = c5.number_input("Other charge (₹)", step=1.0, value=float(b.get("adjustment_amount") or 0))
        c6, c7, c8, c9 = st.columns(4)
        md = c6.number_input("Maximum demand (kVA) (optional)", min_value=0.0, step=0.1, format="%.1f",
                             value=float(b.get("max_demand_kva") or 0))
        md_date = c7.date_input("MD date", value=md_at.date(), format="DD-MM-YYYY")
        md_time = c8.time_input("MD time", value=md_at.time(), step=900)
        cd = c9.number_input("Contract demand (kVA)", min_value=1.0, step=1.0,
                             value=float(b.get("contract_demand_kva") or CONTRACT_DEMAND_KVA))
        note = st.text_input("Grid export settlement", value=b.get("export_note") or DEFAULT_EXPORT_NOTE)
        st.markdown("**Energy totals on the bill** (the dashboard spreads these over the days)")
        e1, e2, e3 = st.columns(3)
        lt_units = e1.number_input(f"SRTPV (LT) meter units (× {LT_METER_MF})", min_value=0.0, step=0.1,
                                   format="%.1f", value=float(b.get("lt_units") or 0))
        ht_imp = e2.number_input("Total HT import (kWh)", min_value=0.0, step=1.0, value=float(b.get("ht_import_kwh") or 0))
        ht_exp = e3.number_input("HT export (kWh)", min_value=0.0, step=1.0, value=float(b.get("ht_export_kwh") or 0))
        st.markdown("**LT meter readings for the month** (20 KW LT + 16 KW LT meters on the bill, "
                    "not the SRTPV solar generation above)")
        bill_lt = st.number_input("Bill LT meter reading for the month (kWh)", min_value=0.0, step=0.1, format="%.1f",
                                  value=float(b.get("bill_lt_kwh") or 0),
                                  help="Deducted from the total HT import (the result is the HT import shown) and from the "
                                       "HT export (HT export - LT meter reading). Leave 0 if not available; the dashboard "
                                       "then shows the small disclaimer instead.")
        meter_note = st.text_input("HT meter note (optional)", value=b.get("meter_note") or "",
                                   placeholder="e.g. New bi-directional meter, readings 02 Sep–01 Oct")
        st.markdown("**Inverter issue for the month** (optional, shown next to 'Month total by inverter')")
        n1, n2 = st.columns([1, 2])
        inv_t = n1.text_input("Title", value=b.get("inverter_note_title") or "",
                              placeholder="e.g. PV7 had internet issues on 28–29 Sep")
        inv_b = n2.text_input("Details", value=b.get("inverter_note_body") or "",
                              placeholder="e.g. Internet link changed to air fiber")
        if st.form_submit_button("Save bill", type="primary"):
            if not (ht_imp or lt_units or net or md):
                st.error("Enter at least the energy totals (or the net payable / maximum demand).")
            elif ht_exp > ht_imp:
                st.error(f"HT export ({ht_exp:,.0f}) is larger than HT import ({ht_imp:,.0f}). "
                         "Please check the two are not swapped.")
            elif bill_lt and bill_lt >= ht_imp:
                st.error("Bill LT must be less than the total HT import.")
            else:
                db.save_bill(month_sel, {
                    "net_payable": net or None, "pf_penalty": pen or None, "adjustment_label": adj_label.strip(),
                    "adjustment_amount": adj if adj_label.strip() else None, "avg_pf": pf or None,
                    "max_demand_kva": md or None, "md_at": dt.datetime.combine(md_date, md_time) if md else None,
                    "contract_demand_kva": cd, "export_note": note.strip(),
                    "lt_units": lt_units or None, "lt_kwh": lt_units * LT_METER_MF if lt_units else None,
                    "ht_import_kwh": ht_imp or None, "ht_export_kwh": ht_exp if ht_imp else None,
                    "bill_lt_kwh": bill_lt if (bill_lt and ht_imp) else None,
                    "meter_note": meter_note.strip(), "inverter_note_title": inv_t.strip(),
                    "inverter_note_body": inv_b.strip(),
                })
                st.success(f"Bill saved for {pd.Timestamp(month_sel + '-01'):%B %Y}")


def _month_label(m: str) -> str:
    return "All months in the file" if m == ALL_MONTHS else pd.Timestamp(m + "-01").strftime("%B %Y")


def _coverage_md(month: str) -> str:
    cov = coverage_text(month, db.coverage(month))
    return (f"**{_month_label(month)}:** Inverter: {cov['inverter']}  \nLT meter: {cov['lt']}  \n"
            f"HT meter: {cov['ht']}")


def import_page() -> None:
    st.subheader("Import data")
    st.caption("Choose the month the readings belong to, or **All months in the file** for a sheet that "
               "covers several months. You can import a month in parts (e.g. 01–15 Oct now, 16–31 Oct "
               "later). Each file is checked first; nothing is saved if a check fails.")
    today = pd.Timestamp.today()
    recent = [p.strftime("%Y-%m") for p in pd.period_range(today - pd.DateOffset(months=11), today, freq="M")]
    month_opts = [ALL_MONTHS] + sorted(set(recent) | set(db.available_months()), reverse=True)
    m1, m2 = st.columns([1, 2])
    data_month = m1.selectbox("Data month", month_opts, key="imp_month", format_func=_month_label)
    mode = m2.radio("If a day is already imported", [ADD_NEW, REPLACE, REPLACE_MONTH], key="imp_mode")
    if data_month != ALL_MONTHS:
        st.markdown("Already imported — " + _coverage_md(data_month))
    if mode == REPLACE_MONTH:
        st.warning("Every month this file covers will be cleared for that sheet type before saving.")

    c1, c2, c3 = st.columns(3)
    f_inv = c1.file_uploader("Inverter daily readings (PV1–PV9)", type=["xlsx", "csv"])
    f_lt = c2.file_uploader("LT generation meter", type=["xlsx", "csv"])
    f_ht = c3.file_uploader("HT import / export meter", type=["xlsx", "csv"])

    if st.button("Validate and save", type="primary", disabled=not (f_inv or f_lt or f_ht)):
        jobs = [(f_inv, import_inverter, db.save_inverter, "Inverter", "inverter"),
                (f_lt, import_lt_meter, db.save_lt, "LT meter", "lt"),
                (f_ht, import_ht_meter, db.save_ht, "HT meter", "ht")]
        touched = set()
        for file, importer, saver, label, kind in jobs:
            if file is None:
                continue
            try:
                res = importer(file)
            except Exception as e:  # unreadable file
                st.error(f"{label}: {e}")
                db.log_import("app", USER["email"], file.name, label, "refused", 0, str(e))
                continue
            for w in res.warnings:
                st.warning(f"{label}: {w}")
            if not res.ok:
                for err in res.errors:
                    st.error(f"{label}: {err}")
                db.log_import("app", USER["email"], file.name, label, "refused", 0, "; ".join(res.errors))
                continue
            parts = split_by_month(kind, res.df) if data_month == ALL_MONTHS else {data_month: res.df}
            for m, part in parts.items():
                tag = f"{label}, {_month_label(m)}"
                last_close = db.lt_closing_before(part["from_date"].min().date()) if kind == "lt" and len(part) else None
                df, errors, notes = prepare_batch(kind, part, m, db.coverage(m)[kind], mode, last_close)
                if errors:
                    for err in errors:
                        st.error(f"{tag}: {err}")
                    db.log_import("app", USER["email"], file.name, label, "refused", 0, "; ".join(errors))
                    continue
                if mode == REPLACE_MONTH:
                    gone = db.clear_month(m, [kind]).get(kind, 0)
                    notes.insert(0, f"Cleared the month's old {label} data ({gone} record(s))")
                for n in notes:
                    st.info(f"{tag}: {n}")
                if df.empty:
                    st.info(f"{tag}: nothing new to add; every day is already imported.")
                    db.log_import("app", USER["email"], file.name, label, "skipped", 0, "; ".join(notes))
                    continue
                saver(df)
                touched.add(m)
                st.success(f"{tag}: {len(df)} row(s) saved")
                db.log_import("app", USER["email"], file.name, label, "saved", len(df),
                              "; ".join(notes + res.warnings))
        for m in sorted(touched):
            st.markdown("Now imported — " + _coverage_md(m))

    with st.expander("Import history (app, email bot and weather bot)"):
        st.dataframe(db.recent_imports(50), hide_index=True, use_container_width=True)

    bill_form()
    if not IS_ADMIN:
        return
    with st.expander("Tools: projected view share per month"):
        st.caption("In the Projected view, an inverter that is faulty on most days of a month gets this share of "
                   "each day's actual total; every reading is scaled down so day totals stay the same. "
                   f"Months without a saved share use {PROJECTION_SHARE:.0%}. To match a monthly report, enter "
                   "the share that report used (September 2026: 10%, October 2026: 7%).")
        shares = projection_shares()
        all_months = db.available_months() or [pd.Timestamp.today().strftime("%Y-%m")]
        s1, s2, s3 = st.columns([1, 1, 1])
        sm = s1.selectbox("Month", all_months, key="share_month", format_func=_month_label)
        val = s2.number_input("Share (%)", min_value=0.0, max_value=30.0, step=0.5,
                              value=round(share_for(sm, shares) * 100, 2), key=f"share_val_{sm}")
        s3.markdown("&nbsp;")
        if s3.button("Save share", key="share_save"):
            db.save_projection_share(sm, val / 100)
            st.success(f"{_month_label(sm)}: share set to {val:g}%")

    with st.expander("Tools: delete a month's data"):
        st.caption(f"Database: {db.database_label()}. Use this to remove test data or a wrong import. "
                   "It cannot be undone.")
        months = db.available_months()
        if not months:
            st.info("No data saved yet.")
            return
        t1, t2 = st.columns([1, 2])
        del_month = t1.selectbox("Month", months, key="del_month", format_func=_month_label)
        names = {"inverter": "Inverter readings and weather", "lt": "LT meter", "ht": "HT meter", "bill": "HT bill"}
        kinds = t2.multiselect("Delete", list(names), default=["inverter"], format_func=names.get, key="del_kinds")
        sure = st.checkbox(f"Yes, delete the selected data for {_month_label(del_month)}", key="del_sure")
        if st.button("Delete", disabled=not (sure and kinds)):
            gone = db.clear_month(del_month, kinds)
            st.success("Deleted: " + ", ".join(f"{names[k]} ({n})" for k, n in gone.items()))
            db.log_import("app", USER["email"], "-", "Delete", "deleted", sum(gone.values()),
                          f"{del_month}: {', '.join(kinds)}")


def users_page() -> None:
    st.subheader("User access")
    st.markdown(
        "**Step 1:** create the person's login in Supabase → *Authentication → Users → Add user* "
        "(tick *Auto confirm user*).  \n"
        "**Step 2:** give that email a role here.  \n"
        "• **Asase O&M Manager**: full dashboard, imports data, manages users  \n"
        "• **Asase O&M Executive**: O&M dashboard, imports data  \n"
        "• **Customer**: dashboard only"
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
    VIEWS = views_for(ROLE)
    VIEW = (st.radio("Dashboard view", VIEWS, horizontal=True, key="view")
            if len(VIEWS) > 1 else VIEWS[0])
    st.header("Filters")

    # ---- period: month, date range or a single day
    # customers pick a month only; staff can also use a date range or a single day
    mode = "Month" if IS_CUSTOMER else st.radio("Period", ["Month", "Date range", "Single day"],
                                                horizontal=True, key="f_mode")
    if mode == "Month":
        months = db.available_months()
        month = st.selectbox("Month", months, format_func=lambda m: pd.Timestamp(m + "-01").strftime("%B %Y"),
                             key="f_month")
        P_START = pd.Timestamp(month + "-01")
        P_END = P_START + pd.offsets.MonthEnd(0)
    elif mode == "Date range":
        month = None
        picked = st.date_input("From – to", value=(bounds[0], bounds[1]),
                               min_value=bounds[0], max_value=bounds[1], format="DD-MM-YYYY", key="f_range")
        if not isinstance(picked, tuple) or len(picked) != 2:
            st.info("Pick an end date.")
            st.stop()
        P_START, P_END = pd.Timestamp(picked[0]), pd.Timestamp(picked[1])
    else:
        month = None
        one = st.date_input("Day", value=bounds[1], min_value=bounds[0], max_value=bounds[1],
                            format="DD-MM-YYYY", key="f_day")
        P_START = P_END = pd.Timestamp(one)

    # Whole months are loaded so bill-based daily figures are spread correctly, then cut to the period.
    L_START = P_START.replace(day=1)
    L_END = P_END + pd.offsets.MonthEnd(0)
    inv_all, lt_all, ht_all = db.load_range(L_START.date(), L_END.date())
    if inv_all.empty:
        st.warning("No data in this period.")
        st.stop()

    # ---- inverters: for staff the selection drives every inverter figure on the page.
    # Customers have no inverter filter here; theirs sits in the Inverter view tab and only changes that table.
    if IS_CUSTOMER:
        SEL = INVERTER_IDS
    else:
        shown_inverters = st.multiselect("Inverters", INVERTER_IDS, default=INVERTER_IDS,
                                         format_func=lambda p: f"{p} · {INVERTERS[p]}", key="f_inv")
        SEL = [p for p in INVERTER_IDS if p in shown_inverters] or INVERTER_IDS
    ALL_SEL = len(SEL) == len(INVERTER_IDS)
    RAW_INV = inv_all.copy()                       # actual readings, for the actual-vs-projected table
    SHARES = projection_shares()
    viewed = apply_view(inv_all, VIEW, SHARES)
    

    # TEMPORARY BYPASS: Show actual inverter readings on 03-Nov-2026
    if VIEW != "Actual":
        nov_3_mask = (
            pd.to_datetime(RAW_INV["date"]).dt.date
            == pd.Timestamp("2026-11-03").date()
        )

        for p in INVERTER_IDS:
         viewed.loc[nov_3_mask, p] = RAW_INV.loc[nov_3_mask, p]
    if not ALL_SEL:
        viewed["Total"] = viewed[SEL].sum(axis=1, min_count=1).round(2)
    inv_all = enrich(viewed, PLANT_CAPACITY_KWP * len(SEL) / len(INVERTER_IDS))
    IN_PERIOD = inv_all[(inv_all["date"] >= P_START) & (inv_all["date"] <= P_END)]
    if IN_PERIOD.empty:
        st.warning("No inverter data in this period.")
        st.stop()

    # ---- weather, performance, day type, health, grouping (staff only; customers filter by period only)
    HEALTH, GROUP = "All days", "Day"
    weather = productivity = day_types = None           # None = no filter
    if not IS_CUSTOMER:
        weather_opts = [w for w in WEATHER_GROUPS if w in set(IN_PERIOD["weather_group"])]
        weather = st.multiselect("Weather", weather_opts, default=weather_opts, key="f_weather")
        band_names = [b[0] for b in PRODUCTIVITY_BANDS]
        band_help = ", ".join(f"{n} {lo:g}–{hi:g}" if hi != float("inf") else f"{n} ≥ {lo:g}"
                              for n, lo, hi in PRODUCTIVITY_BANDS)
        productivity = st.multiselect("Performance (kWh per kWp per day)", band_names, default=band_names,
                                      help=band_help, key="f_prod")
        day_types = st.multiselect("Days", ["Weekday", "Weekend"], default=["Weekday", "Weekend"], key="f_days")
        HEALTH = st.selectbox("Inverter health", ["All days", "Days with a faulty reading",
                                                  "Days with all readings normal"], key="f_health",
                              help="Faulty = blank, or below 70% of the other inverters in its group "
                                   "(selected inverters only).")
        if len(month_list(P_START, P_END)) > 1:
            GROUP = st.radio("Show tables per", ["Day", "Month"], horizontal=True, key="f_group")
        if st.button("Reset filters"):
            for k in [k for k in st.session_state if str(k).startswith("f_")]:
                del st.session_state[k]
            st.rerun()

# =====================================================================
# Period data: bill estimates where a bill has totals, meter readings otherwise
# =====================================================================
BILLS = fill_bill_lt({m: db.load_bill(m) for m in month_list(L_START, L_END)}, lt_all)
lt_p, ht_p, DAILY_ALL, NODATA_ALL, BILL_MONTHS = build_period(inv_all, lt_all, ht_all, BILLS, L_START, L_END)

base = inv_all
if HEALTH != "All days":
    bad = faulty_cells(RAW_INV)
    faulty_days = set(RAW_INV.loc[bad[SEL].any(axis=1).values, "date"])
    keep = base["date"].isin(faulty_days)
    base = base[keep if HEALTH == "Days with a faulty reading" else ~keep]
inv, lt, ht = apply_filters(base, lt_p, ht_p, P_START, P_END, weather, productivity, day_types)
N_PERIOD = len(IN_PERIOD)
filtered = len(inv) < N_PERIOD


def _in_period(df: pd.DataFrame, col: str) -> pd.DataFrame:
    return df[(df[col] >= P_START) & (df[col] <= P_END)].reset_index(drop=True)


if not filtered:                       # keep no-data days (e.g. 01 Sep) in LT/HT totals
    lt, ht = _in_period(lt_p, "from_date"), _in_period(ht_p, "date")
    DAILY = _in_period(DAILY_ALL, "date")
    NODATA = [d for d in NODATA_ALL if P_START <= d <= P_END]
else:
    DAILY = DAILY_ALL[DAILY_ALL["date"].isin(inv["date"])].reset_index(drop=True)
    NODATA = []

if inv.empty:
    st.warning("No days match these filters. Widen the period, weather, performance or health filters.")
    st.stop()
shown_inverters = SEL

first, last = inv["date"].min(), inv["date"].max()
KPIS = headline_kpis(inv, lt, ht)
HT_FROM_BILL: list[str] = []
if not filtered:                       # a month with only HT bill totals (no daily HT readings) uses the bill totals
    KPIS, HT_FROM_BILL = bill_totals_fallback(KPIS, BILLS, BILL_MONTHS, ht, P_START, P_END)
GRID = grid_summary(ht, lt)
BILL = BILLS.get(month) if month else None
BILL_BASED = bool(month) and month in BILL_MONTHS
MONTH_NAME = pd.Timestamp(month + "-01").strftime("%B") if month else None
IS_PROJ = VIEW == PROJECTED
DATA_WORD = "projected" if IS_PROJ else "actual"
SEL_NOTE = "" if ALL_SEL else " · " + ", ".join(SEL)


# =====================================================================
# Look and feel (matches the asase.energy monthly report)
# =====================================================================
def _img64(name: str) -> str:
    path = Path(__file__).parent / "assets" / name
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else ""


LOGO_MARK, LOGO_WORD = _img64("logo_mark.png"), _img64("logo_word.png")

ICONS = {
    "panel": '<rect x="3" y="5" width="18" height="11" rx="1"/><path d="M3 10.5h18M9 5v11M15 5v11M12 16v4M8 20h8"/>',
    "gauge": '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l4-6"/>',
    "down": '<path d="M12 5v14M5 12l7 7 7-7"/>',
    "up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "factory": '<path d="M3 20V10l6 4v-4l6 4V6h6v14z"/>',
    "pie": '<path d="M12 3v9h9A9 9 0 1 1 12 3z"/>',
    "bolt": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    "pf": '<path d="M3 12h4V6h5v12h5v-6h4"/>',
    "rain": '<path d="M7 15a4 4 0 0 1 .5-8 5 5 0 0 1 9.5 1.5A3.5 3.5 0 0 1 17 15z"/><path d="M8 19v1M12 19v1M16 19v1"/>',
    "calc": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 7h8M8 12h1M12 12h1M16 12h0M8 16h1M12 16h1M16 16h0"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
    "warn": '<path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17v.5"/>',
}
C = {"amber": "#E5A01A", "teal": "#2A9D8F", "navy": "#3D5A80", "coral": "#E07B5F", "slate": "#3B4A5A",
     "red": "#C0392B", "ink": "#1C2B3A"}

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@400;600;700&display=swap');
.ae-serif{font-family:'Source Serif 4',Cambria,Georgia,serif}
.ae-cover{background:#1C2B3A;border-radius:14px;padding:30px 38px 22px;color:#fff;margin-bottom:8px;text-align:center}
.ae-logo{display:inline-flex;align-items:center;gap:12px;background:#fff;padding:6px 14px;border-radius:4px}
.ae-cover h1{font-family:'Source Serif 4',Cambria,Georgia,serif;font-weight:700;color:#fff;font-size:2.7rem;margin:0;padding:0}
.ae-cover .sub{color:#E5A01A;font-family:'Source Serif 4',Cambria,Georgia,serif;font-size:1.7rem;margin-top:2px}
.ae-cover .meta{color:#C9D2DC;margin-top:14px}
.ae-hero{display:flex;gap:70px;flex-wrap:wrap;margin-top:30px;justify-content:center}
.ae-hero .v{font-family:'Source Serif 4',Cambria,Georgia,serif;font-size:2.6rem;color:#fff;line-height:1.1}
.ae-hero .l{color:#DDE3EA;font-size:.95rem}.ae-hero .s{color:#8FA0B3;font-size:.95rem}
.ae-cover .prep{color:#8FA0B3;font-size:.8rem;margin-top:26px}
.ae-h2{font-family:'Source Serif 4',Cambria,Georgia,serif;font-weight:700;color:#1C2B3A;text-align:center;font-size:2.1rem;margin:42px 0 6px}
.ae-lead{color:#3B4A5A;margin:0 0 16px;font-size:1.02rem}
.ae-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.ae-card{background:#F1F3F7;border-radius:12px;padding:20px 22px}
.ae-ico{width:46px;height:46px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:22px;font-weight:700}
.ae-ico svg{width:24px;height:24px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ae-card .v{font-family:'Source Serif 4',Cambria,Georgia,serif;font-size:2.3rem;color:#1C2B3A;margin-top:12px;line-height:1.1}
.ae-card .l{font-weight:700;color:#1C2B3A;margin-top:4px}.ae-card .n{color:#5B6B7B;font-size:.85rem}
.ae-bill{display:grid;grid-template-columns:1fr 1fr 1.3fr;gap:16px}
.ae-why{background:#F9E1DE;border-radius:12px;padding:22px 26px;grid-row:span 2;color:#3B4A5A}
.ae-why h4{display:flex;align-items:center;gap:14px;color:#1C2B3A;font-size:1.25rem;margin:0 0 14px}
.ae-side{display:flex;flex-direction:column;gap:16px}
.ae-side .ae-card h4{display:flex;align-items:center;gap:12px;margin:0 0 8px;color:#1C2B3A;font-size:1.1rem}
.ae-side .ae-card .v{margin-top:0}
.ae-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(86px,1fr));gap:6px;margin-top:6px}
.ae-tile{background:#F1F3F7;border-radius:8px;padding:6px 4px;text-align:center;font-size:.78rem;color:#3B4A5A;line-height:1.35}
.ae-tile b{color:#1C2B3A}.ae-tile.low{background:#F9E1DE}
.ae-tw{overflow-x:auto}
table.ae-t{border-collapse:collapse;width:100%;font-size:.86rem}
.ae-t th{background:#1C2B3A;color:#fff;padding:7px 9px;text-align:right;white-space:nowrap}
.ae-t th:nth-child(-n+2),.ae-t td:nth-child(-n+2){text-align:left}
.ae-t td{padding:6px 9px;text-align:right;border-bottom:1px solid #fff;color:#1C2B3A;white-space:nowrap}
.ae-t td.i{font-style:italic}.ae-t td.b{font-weight:700}.ae-t td.tot{background:#ECEFF4;font-weight:700}
.ae-t tr.low td{color:#C0392B}.ae-t tr.lowbg td{background:#FBE6E3}
.ae-t td.flag{background:#F9E1DE !important;color:#C0392B !important;font-weight:700}
.ae-inv tr.low td{color:#7B8794}.ae-inv td.flag{background:#E9ECEF !important;color:#5B6B7B !important;font-weight:700}
.ae-t.ae-grey tr.lowbg td{background:#E9ECEF}
.ae-calc{margin:8px 0 0 auto;max-width:430px;border-top:2px solid #1C2B3A;padding-top:4px}
.ae-calc table{width:100%;border-collapse:collapse;font-size:.86rem}
.ae-calc td{padding:3px 9px;color:#1C2B3A}.ae-calc td+td{text-align:right;white-space:nowrap}
.ae-calc tr.net td{font-weight:700;border-top:1px solid #1C2B3A}
.ae-calc .sm,.ae-ltnote{color:#5B6B7B;font-size:.72rem}.ae-ltnote{text-align:right;margin-top:4px}
.ae-t td.proj{color:#2A6FB0 !important}.ae-t td.proj::after{content:" p";font-size:.7em;vertical-align:super}
.ae-t tr.total td{background:#E3E7ED;font-weight:700}
.ae-t td.wx{text-align:left;white-space:normal;min-width:180px}.ae-t th.wx{text-align:left}
.ae-key{color:#5B6B7B;font-size:.8rem;margin-top:8px}.ae-scroll{max-height:560px;overflow:auto;border:1px solid #E3E7ED;border-radius:8px}.ae-scroll .ae-t th{position:sticky;top:0;z-index:2}.ae-scroll .ae-t tr.total td{position:sticky;bottom:0;z-index:1}
.ae-obs{background:#1C2B3A;color:#fff;border-radius:14px;padding:30px 36px;margin-top:42px}
.ae-obs h2{font-family:'Source Serif 4',Cambria,Georgia,serif;color:#fff;font-size:2rem;margin:0 0 18px}
.ae-oi{display:flex;gap:18px;margin:16px 0}.ae-oi b{font-size:1.1rem}.ae-oi div div{color:#C9D2DC;margin-top:4px}
.ae-foot{display:flex;justify-content:space-between;align-items:center;color:#5B6B7B;font-size:.75rem;border-top:1px solid #E3E7ED;margin-top:36px;padding-top:10px}
.ae-sw{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:8px;vertical-align:-1px}
table.ae-pv td:first-child,table.ae-pv th:first-child{text-align:left;white-space:normal}
table.ae-pv th:nth-child(2),table.ae-pv td:nth-child(2){text-align:right}
@media (max-width:900px){.ae-grid{grid-template-columns:1fr 1fr}.ae-bill{grid-template-columns:1fr 1fr}.ae-why{grid-row:auto;grid-column:1/-1}.ae-hero{gap:36px}}@media (max-width:640px){.ae-grid{grid-template-columns:1fr 1fr;gap:8px}.ae-bill{grid-template-columns:1fr;gap:10px}.ae-grid .ae-card{padding:12px}.ae-grid .ae-card .v{font-size:1.3rem;margin-top:8px}.ae-grid .ae-card .l{font-size:.85rem}.ae-grid .ae-card .n{font-size:.72rem}.ae-cover{padding:20px 16px 16px}.ae-cover h1{font-size:1.7rem}.ae-cover .sub{font-size:1.1rem}.ae-cover .meta{font-size:.82rem}.ae-hero{gap:18px;margin-top:18px}.ae-hero .v{font-size:1.75rem}.ae-h2{font-size:1.4rem;margin:28px 0 4px}.ae-lead{font-size:.92rem}.ae-card{padding:14px 16px}.ae-card .v{font-size:1.7rem}.ae-ico{width:38px;height:38px;font-size:18px}.ae-ico svg{width:20px;height:20px}.ae-why{padding:16px 18px}table.ae-t{font-size:.76rem}.ae-t th,.ae-t td{padding:5px 6px}.ae-t td.wx{min-width:120px}.ae-scroll{max-height:420px}.ae-tiles{grid-template-columns:repeat(auto-fill,minmax(68px,1fr))}.ae-tile{font-size:.7rem}.ae-obs{padding:20px 18px}.ae-obs h2{font-size:1.4rem}.ae-foot{flex-direction:column;gap:6px;align-items:flex-start}}
</style>""", unsafe_allow_html=True)


def ico(name: str, colour: str) -> str:
    inner = "₹" if name == "rupee" else f'<svg viewBox="0 0 24 24">{ICONS[name]}</svg>'
    return f'<div class="ae-ico" style="background:{C[colour]}">{inner}</div>'


def card(icon: str, colour: str, value: str, label: str, note: str) -> str:
    return (f'<div class="ae-card">{ico(icon, colour)}<div class="v">{escape(value)}</div>'
            f'<div class="l">{escape(label)}</div><div class="n">{escape(note)}</div></div>')


def h2(title: str, lead: str = "") -> None:
    st.markdown(f'<div class="ae-h2">{escape(title)}</div>'
                + (f'<div class="ae-lead">{escape(lead)}</div>' if lead else ""), unsafe_allow_html=True)


def _span(a: pd.Timestamp, b: pd.Timestamp) -> str:
    if a == b:
        return f"{a:%d} {a:%b}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%d}–{b:%d} {b:%b}"
    return f"{a:%d} {a:%b} – {b:%d} {b:%b}"


def _short(a: pd.Timestamp, b: pd.Timestamp) -> str:          # "2–30 Sep" (no leading zero)
    if a == b:
        return f"{a.day} {a:%b}"
    return f"{a.day}–{b.day} {b:%b}" if (a.year, a.month) == (b.year, b.month) else _span(a, b)


def src_notes() -> tuple[str, str]:
    if BILL_BASED:
        return f"kWh · SRTPV meter, {MONTH_NAME} bill", f"kWh · {MONTH_NAME} bill"
    return "kWh · SRTPV (LT) meter", "kWh · HT meter"


# =====================================================================
# Sections
# =====================================================================
def page_logo() -> None:
    st.markdown(_logo_html(), unsafe_allow_html=True)


def customer_title() -> None:
    title = f"{MONTH_NAME} {P_START:%Y} generation and grid report" if month else \
        f"Generation and grid report, {_span(first, last)} {last:%Y}"
    st.markdown(f'<div class="ae-title">{escape(PLANT_NAME)}</div><div class="ae-subtitle">{escape(title)}</div>'
                f'<div class="ae-small">{PLANT_CAPACITY_KWP} kWp rooftop solar</div>', unsafe_allow_html=True)


def cover() -> None:
    title = f"{MONTH_NAME} {P_START:%Y} generation and grid report" if month else \
        f"Generation and grid report, {_span(P_START, P_END)} {P_END:%Y}"
    src = (f"LT and HT totals from the {MONTH_NAME} bill" if BILL_BASED else "LT and HT from meter readings")
    hero = [(f"{KPIS['inverter_kwh']:,.0f}", "kWh from inverters" + ("" if ALL_SEL else f" ({', '.join(SEL)})"),
             f"{_short(first, last)}, {DATA_WORD}"),
            (f"{KPIS['ht_import_kwh']:,.0f}", "kWh imported from grid",
             f"{MONTH_NAME} bill" if BILL_BASED else "HT meter")]
    if BILL and BILL.get("max_demand_kva") and BILL.get("md_at"):
        md_at = pd.Timestamp(BILL["md_at"])
        hero.append((f"{BILL['max_demand_kva']:,.1f} kVA", "maximum demand", f"{md_at.day} {md_at:%b}, {md_at:%H:%M}"))
    else:
        hero.append((f"{KPIS['solar_share']:.0%}", "solar share of load", "LT solar ÷ site consumption"))
    st.markdown(
        f'<div class="ae-cover"><h1>{escape(PLANT_NAME)}</h1><div class="sub">{escape(title)}</div>'
        f'<div class="meta">{PLANT_CAPACITY_KWP} kWp rooftop solar · Inverter data {DATA_WORD} · {escape(src)}</div>'
        '<div class="ae-hero">' + "".join(
            f'<div><div class="v">{v}</div><div class="l">{l}</div><div class="s">{s}</div></div>' for v, l, s in hero)
        + f'</div><div class="prep">Prepared {pd.Timestamp.today():%d %B %Y}</div></div>', unsafe_allow_html=True)
    if filtered:
        st.info(f"Filters on: showing **{len(inv)} of {N_PERIOD} days**. Figures cover only these days.")


def at_a_glance(customer: bool = False) -> None:
    lt_note, ht_note = src_notes()
    imp_note = f"kWh · HT meter · {BILL_LT_DISCLAIMER}"
    h2(f"{MONTH_NAME} at a glance" if month else f"At a glance, {_span(first, last)}",
       glance_headline(KPIS, inv, "month" if month else "period", include_outages=not customer, only=SEL))
    cards_html = [
        card("panel", "amber", f"{KPIS['inverter_kwh']:,.0f}", "Inverter generation", (f"kWh · {_short(first, last)}{SEL_NOTE}" if IS_CUSTOMER else f"kWh · {DATA_WORD} · {_short(first, last)}{SEL_NOTE}")),
        card("gauge", "teal", f"{KPIS['lt_kwh']:,.0f}", "LT meter generation", lt_note),
        card("down", "navy", f"{KPIS['ht_import_kwh']:,.0f}", "HT grid import", imp_note),
        card("up", "coral", f"{KPIS['ht_export_kwh']:,.0f}", "HT grid export", ht_note),
        card("factory", "slate", f"{KPIS['site_load_kwh']:,.0f}", "Site consumption", "kWh · HT net import + LT solar"),
        card("pie", "amber", f"{KPIS['solar_share']:.0%}", "Solar share of load", "LT solar ÷ site consumption"),
    ]
    st.markdown('<div class="ae-grid">' + "".join(cards_html) + "</div>", unsafe_allow_html=True)
    if HT_FROM_BILL:
        names = ", ".join(pd.Timestamp(m + "-01").strftime("%B") for m in HT_FROM_BILL)
        st.caption(f"HT grid import and export for {names} are the HT bill totals (no date-wise HT meter readings).")

def bill_slide() -> None:
    if not month:
        return
    if not BILL:
        if CAN_IMPORT:
            st.info(f"No HT bill entered for {MONTH_NAME} yet. Add it under Import data → Monthly HT bill.")
        return
    has_md = BILL.get("max_demand_kva") and BILL.get("md_at") and BILL.get("contract_demand_kva")
    if not (BILL.get("net_payable") or has_md or BILL.get("avg_pf")):
        return                          # bill saved with energy totals only: nothing to showcase here
    h2(f"{MONTH_NAME} {P_START:%Y} bill", demand_headline(BILL, inv_all) if has_md else "")
    extras = []
    if BILL.get("pf_penalty"):
        extras.append(f"{inr(BILL['pf_penalty'])} PF penalty")
    if BILL.get("adjustment_label") and BILL.get("adjustment_amount"):
        extras.append(f"{inr(BILL['adjustment_amount'])} {BILL['adjustment_label']}")
    pay = card("rupee", "slate", inr(BILL.get("net_payable")), "Net payable, HT bill",
               ("Includes " + " and ".join(extras)) if extras else "")
    md = ""
    if has_md:
        md_at = pd.Timestamp(BILL["md_at"])
        pct = demand_pct(BILL["max_demand_kva"], BILL["contract_demand_kva"])
        md = card("bolt", "red", f"{BILL['max_demand_kva']:,.1f} kVA", "Maximum demand",
                  f"{md_at:%d-%m-%Y %H:%M} · {pct:.1f}% of {BILL['contract_demand_kva']:,.0f} kVA contract")
    pf = BILL.get("avg_pf")
    if pf is None:
        pf_card = card("pf", "coral", "—", "Average power factor", "not on bill")
    elif pf < PF_LIMIT:
        pf_card = card("pf", "coral", f"{pf:.3f}", "Average power factor", f"Below {PF_LIMIT:.2f}"
                       + (f" · {inr(BILL['pf_penalty'])} penalty" if BILL.get("pf_penalty") else ""))
    else:
        pf_card = card("pf", "coral", f"{pf:.3f}", "Average power factor", f"At or above {PF_LIMIT:.2f} · no penalty")
    exp = BILL.get("ht_export_kwh") if BILL.get("ht_export_kwh") is not None else KPIS["ht_export_kwh"]
    exp_card = card("up", "teal", f"{exp:,.0f} kWh", "Grid export", "")
    story = demand_story(BILL, inv_all, MONTH_NAME) if has_md else None
    why = ""
    if story:
        first_sentence, _, rest = story.partition(". ")
        why = (f'<div class="ae-why"><h4>{ico("rain", "red")}Why the demand peaked</h4>'
               f'<p>{escape(first_sentence)}.</p><p>{escape(rest)}</p></div>')
    st.markdown(f'<div class="ae-bill">{pay}{md}{why}{pf_card}{exp_card}</div>', unsafe_allow_html=True)


def daily_generation() -> None:
    h2("Daily inverter generation", daily_headline(inv, NODATA))
    st.plotly_chart(charts.report_daily_chart(DAILY), use_container_width=True)
    low_days = set(DAILY.loc[very_low_mask(DAILY["inverter_kwh"].dropna()).reindex(DAILY.index, fill_value=False), "date"])
    tiles = []
    for r in DAILY.itertuples():
        nod = r.no_data
        label = "No data" if nod else short_weather(r.weather)
        temp = "—" if nod or pd.isna(r.temp_c) else f"{r.temp_c:.1f}°C"
        cls = "ae-tile low" if r.date in low_days else "ae-tile"
        tiles.append(f'<div class="{cls}"><b>{r.date:%d %b %a}</b><br>{escape(label)}<br>{temp}</div>')
    st.markdown('<b style="color:#1C2B3A">Weather</b><div class="ae-tiles">' + "".join(tiles) + "</div>",
                unsafe_allow_html=True)


def inverter_vs_lt() -> None:
    if not ALL_SEL:
        h2("Inverter vs LT meter")
        st.info("The LT meter measures all nine inverters together, so this comparison is shown only "
                "when all inverters are selected.")
        return
    lt_est = BILL_BASED
    h2("Inverter vs LT meter")
    c1, c2 = st.columns([2.2, 1])
    with c1:
        st.plotly_chart(charts.inverter_vs_lt_chart(DAILY, lt_est, f"Inverter ({DATA_WORD})"), use_container_width=True)
    ratio = KPIS["lt_kwh"] / KPIS["inverter_kwh"] if KPIS["inverter_kwh"] else float("nan")
    nod_txt = (f" {', '.join(f'{d:%d %b}' for d in NODATA)} has no inverter data, so the true ratio is slightly lower."
               if NODATA else "")
    side = [f'<div class="ae-card"><h4>{ico("calc", "teal")}Ratio about {ratio:.3f}</h4><div class="n">'
            f'Actual LT {KPIS["lt_kwh"]:,.0f} ÷ inverter {KPIS["inverter_kwh"]:,.0f} kWh.{escape(nod_txt)}</div></div>']
    if BILL_BASED and BILL.get("lt_units"):
        side.append(f'<div class="ae-card"><h4>{ico("gauge", "amber")}{KPIS["lt_kwh"]:,.0f} kWh</h4><div class="n">'
                    f'SRTPV meter for {MONTH_NAME}: {BILL["lt_units"]:,.1f} × meter constant {LT_METER_MF}.</div></div>')
    if lt_est:
        side.append(f'<div class="ae-card"><h4>{ico("search", "slate")}Daily values estimated</h4><div class="n">'
                    "The bill gives only a month total, so daily LT values"
                    + (f", including {', '.join(f'{d:%d %b}' for d in NODATA)}," if NODATA else "")
                    + " remain estimates.</div></div>")
    else:
        side.append(f'<div class="ae-card"><h4>{ico("search", "slate")}Daily LT readings</h4><div class="n">'
                    "Daily values are meter readings; combined periods show no daily point.</div></div>")
    with c2:
        st.markdown('<div class="ae-side">' + "".join(side) + "</div>", unsafe_allow_html=True)


def pv_system_comparison() -> None:
    """Daily energy stacked by PV system, with each system's kWh/kWp and kWh for the period
    (like the inverter portal's PV system comparison). Always covers all nine inverters."""
    time_range = f"{MONTH_NAME} {P_START:%Y}" if month else f"{_span(first, last)} {last:%Y}"
    word = "" if IS_CUSTOMER else f", {DATA_WORD}"
    h2("PV system comparison", f"Daily energy by PV system, {time_range}{word}")
    cmp = pv_comparison(inv)
    rows = "".join(
        f'<tr><td><span class="ae-sw" style="background:{charts.PV_COLOURS[r.inverter]}"></span>'
        f'{escape(r.name)}</td><td>{r.kwh_per_kwp:,.2f}</td><td>{r.kwh:,.2f}</td></tr>'
        for r in cmp.itertuples())
    c1, c2 = st.columns([1, 2.3])
    with c1:
        st.markdown(f'<div class="ae-key" style="margin:0 0 6px">Time range: <b>{escape(time_range)}</b></div>'
                    '<div class="ae-tw"><table class="ae-t ae-pv"><tr><th>PV system</th><th>kWh/kWp</th><th>kWh</th></tr>'
                    + rows + "</table></div>", unsafe_allow_html=True)
    with c2:
        st.plotly_chart(charts.pv_comparison_chart(inv), use_container_width=True)


def inverter_vs_lt_bars() -> None:
    """Daily inverter total next to the LT meter reading, as bars."""
    h2("Inverter vs LT meter", "Daily inverter generation against the LT meter")
    st.plotly_chart(charts.inverter_vs_lt_bar_chart(
        DAILY, BILL_BASED, "Inverter" if IS_CUSTOMER else f"Inverter ({DATA_WORD})"), use_container_width=True)
    gaps = DAILY.loc[DAILY["lt_kwh"].isna() & DAILY["inverter_kwh"].notna(), "date"]
    if len(gaps):
        st.caption(f"No LT meter reading for {day_ranges(gaps)}.")


def _month_rows(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Daily rows -> one row per month (sums), for 'Show tables per: Month'."""
    g = df.groupby(df["date"].dt.to_period("M"))
    out = g[cols].sum(min_count=1)
    out["days"] = g.size()
    out = out.reset_index()
    out["label"] = out["date"].dt.strftime("%B %Y")
    return out


def _with_total(df: pd.DataFrame, label_col: str, sum_cols: list[str], label: str = "Total") -> pd.DataFrame:
    """Table for Excel: the rows as shown plus a closing total row."""
    tot = {c: "" for c in df.columns}
    tot[label_col] = label
    for c in sum_cols:
        tot[c] = df[c].sum(min_count=1)
    return pd.concat([df, pd.DataFrame([tot])], ignore_index=True)


def excel_download(df: pd.DataFrame, sheet: str, stem: str) -> None:
    """'Download Excel' button under a table. The file holds the same rows the table shows."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name=sheet[:31], index=False)
        ws = xw.sheets[sheet[:31]]
        for i, c in enumerate(df.columns, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(12, min(28, len(str(c)) + 4))
    when = month or f"{first:%Y%m%d}-{last:%Y%m%d}"
    st.download_button("Download Excel", buf.getvalue(), file_name=f"srtpv_{stem}_{when}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       key=f"dl_{stem}")


def _day_cols(df: pd.DataFrame, extra: list[str]) -> pd.DataFrame:
    out = df[["date", "day", *extra]].copy()
    out["date"] = out["date"].dt.strftime("%d %b %Y")
    return out


def _inv_table_html(cols: list[str]) -> str:
    """One scrollable table for the whole period (header and total row stay in view).
    The Total column is the sum of the inverters shown (the plant total when all are shown)."""
    by_month = GROUP == "Month"
    src = inv if len(cols) == len(INVERTER_IDS) else inv.assign(Total=inv[cols].sum(axis=1, min_count=1).round(2))
    head = "".join(f"<th>{c}</th>" for c in (["Month", "Days"] if by_month else ["Date", "Day"]) + cols
                   + ["Total" if src is inv else "Total (shown)"])
    body = []
    if by_month:
        m = _month_rows(src, [*cols, "Total"])
        vmax = float(m[cols].to_numpy().max() or 1)
        for _, r in m.iterrows():
            body.append(f'<tr><td class="b">{r["label"]}</td><td>{int(r["days"])}</td>'
                        + "".join(f'<td class="i" style="background:rgba(229,160,26,{0.10 + 0.32 * (r[p] or 0) / vmax:.2f})">'
                                  f'{"—" if pd.isna(r[p]) else f"{r[p]:,.2f}"}</td>' for p in cols)
                        + f'<td class="tot">{r["Total"]:,.2f}</td></tr>')
    else:
        vmax = float(inv[cols].to_numpy(dtype=float, na_value=0).max() or 1)
        flags = flagged_cells(inv)
        low = very_low_mask(src["Total"])
        multi_year = first.year != last.year
        for i, r in inv.iterrows():
            tds = [f'<td class="b">{r["date"]:%d %b %Y}</td>' if multi_year else f'<td class="b">{r["date"]:%d %b}</td>',
                   f"<td>{r['day']}</td>"]
            for p in cols:
                v = r[p]
                if pd.isna(v):
                    tds.append('<td class="i">—</td>')
                elif flags.at[i, p] and not IS_PROJ and not IS_CUSTOMER:
                    tds.append(f'<td class="i flag">{v:,.2f}</td>')
                else:
                    mark = " proj" if (IS_PROJ and not IS_CUSTOMER and bool(r.get(f"proj_{p}", False))) else ""
                    tds.append(f'<td class="i{mark}" style="background:rgba(229,160,26,{0.10 + 0.32 * v / vmax:.2f})">'
                               f"{v:,.2f}</td>")
            tot = src.at[i, "Total"]
            tds.append(f'<td class="tot">{"—" if pd.isna(tot) else f"{tot:,.2f}"}</td>')
            body.append(f'<tr class="{"low" if low[i] else ""}">' + "".join(tds) + "</tr>")
    sums = src[[*cols, "Total"]].sum()
    body.append(f'<tr class="total"><td colspan="2">Total {_short(first, last)}</td>'
                + "".join(f'<td class="i">{sums[c]:,.2f}</td>' for c in cols) + f'<td>{sums["Total"]:,.2f}</td></tr>')
    return f'<div class="ae-scroll"><table class="ae-t ae-inv"><tr>{head}</tr>{"".join(body)}</table></div>'


def _roof_key() -> str:
    groups: dict[str, list[str]] = {}
    for p in INVERTER_IDS:
        groups.setdefault(INVERTERS[p], []).append(p)
    return "  ·  ".join(f"{ps[0]}–{ps[-1]} {loc}" if len(ps) > 2 else f"{', '.join(ps)} {loc}"
                        for loc, ps in groups.items())


def inverter_readings(with_filter: bool = False) -> None:
    """with_filter (customer portal): an inverter picker above the table. It changes only this table;
    every other figure on the page keeps covering all inverters."""
    cols = shown_inverters
    per = " (per month)" if GROUP == "Month" else ""
    h2(f"Inverter-wise readings, {_span(first, last)}{per}")
    if with_filter:
        picked = st.multiselect("Inverters", INVERTER_IDS, default=INVERTER_IDS,
                                format_func=lambda p: f"{p} · {INVERTERS[p]}", key="inv_view_pick")
        cols = [p for p in INVERTER_IDS if p in picked] or INVERTER_IDS
    st.markdown(_inv_table_html(cols), unsafe_allow_html=True)
    src = inv if len(cols) == len(INVERTER_IDS) else inv.assign(Total=inv[cols].sum(axis=1, min_count=1).round(2))
    tot_name = "Total" if src is inv else "Total (shown)"
    if GROUP == "Month":
        x = _month_rows(src, [*cols, "Total"])[["label", "days", *cols, "Total"]].rename(columns={"label": "Month", "days": "Days"})
        x = _with_total(x, "Month", [*cols, "Total"])
    else:
        x = _day_cols(src, [*cols, "Total"]).rename(columns={"date": "Date", "day": "Day"})
        x = _with_total(x, "Date", [*cols, "Total"])
    excel_download(x.rename(columns={"Total": tot_name}), "Inverter view", "inverter_view")
    key = _roof_key()
    if len(cols) < len(INVERTER_IDS):
        key += "  ·  Total (shown) = sum of the inverters shown"
    if NODATA and not IS_CUSTOMER:
        key += "  ·  " + ", ".join(f"{d:%d %b}" for d in NODATA) + ": no data"
    if IS_PROJ and not IS_CUSTOMER and any(inv[f"proj_{p}"].any() for p in cols if f"proj_{p}" in inv):
        key += (f"  ·  p = includes {share_for(month, SHARES):.0%} of the day's total (day totals unchanged)" if month
                else "  ·  p = includes a share of the day's total (day totals unchanged)")
    st.markdown(f'<div class="ae-key">{escape(key)}</div>', unsafe_allow_html=True)
    if month:                                   # note for the month: written by admins, read by everyone
        inv_note = db.load_inverter_note(month)
        if inv_note:
            st.markdown('<div class="ae-card" style="margin-top:10px"><h4>Note</h4><div class="n">'
                        + escape(inv_note).replace("\n", "<br>") + "</div></div>", unsafe_allow_html=True)
        if IS_ADMIN:
            new_note = st.text_area("Note under the inverter table (admin only, visible to everyone)",
                                    value=inv_note, height=90, key=f"inv_note_{month}")
            if st.button("Save inverter note", key=f"inv_note_save_{month}"):
                db.save_inverter_note(month, new_note.strip())
                st.success("Note saved")
                st.rerun()


def month_total() -> None:
    h2("Month total by inverter" if month else "Total by inverter")
    totals = inverter_totals(inv)
    c1, c2 = st.columns([2.2, 1])
    with c1:
        st.plotly_chart(charts.month_total_chart(totals[totals["inverter"].isin(shown_inverters)]),
                        use_container_width=True)
    note_t = (BILL or {}).get("inverter_note_title") or ""
    note_b = (BILL or {}).get("inverter_note_body") or ""
    if note_t and not IS_PROJ:  # inverter-specific note describes the actual readings
        with c2:
            st.markdown("<div style='height:180px'></div>", unsafe_allow_html=True)
            st.markdown(f'<div class="ae-side"><div class="ae-card"><h4>{ico("warn", "coral")}{escape(note_t)}</h4>'
                        f'<div class="n">{escape(note_b)}</div></div></div>', unsafe_allow_html=True)


def grid_exchange() -> None:
    h2("Grid exchange at the HT meter")
    c1, c2 = st.columns([2.2, 1])
    with c1:
        st.plotly_chart(charts.grid_exchange_report(DAILY), use_container_width=True)
    tag = "bill" if BILL_BASED else "HT meter"
    side = [f'<div class="ae-card"><h4>{ico("down", "navy")}<span class="ae-serif" style="font-size:1.9rem;font-weight:400">'
            f'{KPIS["ht_import_kwh"]:,.0f}</span></h4><div class="n">kWh imported ({tag})</div></div>',
            f'<div class="ae-card"><h4>{ico("up", "coral")}<span class="ae-serif" style="font-size:1.9rem;font-weight:400">'
            f'{KPIS["ht_export_kwh"]:,.0f}</span></h4><div class="n">kWh exported ({tag})</div></div>',
            f'<div class="ae-card"><h4>{ico("bolt", "slate")}<span class="ae-serif" style="font-size:1.9rem;font-weight:400">'
            f'{KPIS["ht_net_kwh"]:,.0f}</span></h4><div class="n">kWh net import</div></div>']
    note = (BILL or {}).get("meter_note") or ""
    with c2:
        st.markdown('<div class="ae-side">' + "".join(side) + "</div>"
                    + (f'<div class="ae-key">{escape(note)}</div>' if note else ""), unsafe_allow_html=True)


def ht_import_calc() -> None:
    """Under the Consolidated and HT tables: Total HT import - Bill LT = HT import.
    If the month's Bill LT is not entered, only the small disclaimer is shown."""
    months = month_list(P_START, P_END)
    b = ht_import_breakdown(BILLS, months)
    if not b["populated"]:
        st.markdown(f'<div class="ae-ltnote">HT import {escape(BILL_LT_DISCLAIMER)}</div>', unsafe_allow_html=True)
    elif not filtered:                  # with weather/health filters on, the table covers only some days
        st.markdown(
            '<div class="ae-calc"><table>'
            f'<tr><td>Total HT import (as per bill)</td><td>{b["gross"]:,.1f}</td></tr>'
            '<tr><td>Less: LT meter reading <span class="sm">(20 KW LT + 16 KW LT)</span></td>'
            f'<td>− {b["bill_lt"]:,.1f}</td></tr>'
            f'<tr class="net"><td>HT import</td><td>{b["net"]:,.1f}</td></tr>'
            "</table></div>", unsafe_allow_html=True)


def consolidated_inverter_note() -> None:
    """Customer Consolidated tab: the month's inverter note (written by admins, same text as under the Inverter view table)."""
    inv_note = db.load_inverter_note(month) if month else ""
    if inv_note:
        st.markdown('<div class="ae-card" style="margin-top:10px"><h4>Note</h4><div class="n">'
                    + escape(inv_note).replace("\n", "<br>") + "</div></div>", unsafe_allow_html=True)


def statement(customer: bool = False) -> None:
    est = BILL_BASED and not customer
    lead = (f"Weather and inverter {DATA_WORD}; LT, HT and site load spread by day from bill totals (italic)"
            if est else ("" if customer else "Weather, inverter and meter readings"))
    star = "*" if est else ""
    meters = ("lt_kwh", "ht_import_kwh", "ht_export_kwh", "ht_net_kwh", "site_load_kwh")
    f0 = lambda v: "—" if pd.isna(v) else f"{v:,.0f}"
    ic = "i" if est else ""
    inv_head = "Inverter kWh" + ("" if ALL_SEL else f" ({', '.join(SEL)})")
    per = " (per month)" if GROUP == "Month" else ""
    h2(f"Consolidated daily statement, {_span(first, last)}{per}", lead)
    rows = []
    if GROUP == "Month":
        cols = ["Month", "Days", inv_head, f"LT meter kWh{star}", f"HT import kWh{star}", f"HT export kWh{star}",
                f"HT net kWh{star}", f"Site load kWh{star}"]
        m = _month_rows(DAILY.assign(inverter_kwh=DAILY["inverter_kwh"]), ["inverter_kwh", *meters])
        for _, r in m.iterrows():
            rows.append(f'<tr><td class="b">{r["label"]}</td><td>{int(r["days"])}</td>'
                        f'<td class="b">{f0(r["inverter_kwh"])}</td>'
                        + "".join(f'<td class="{ic}">{f0(r[c])}</td>' for c in meters) + "</tr>")
        span = 2
    else:
        cols = ["Date", "Day", "Weather", "Temp °C", "Wind m/s", inv_head, f"LT meter kWh{star}",
                f"HT import kWh{star}", f"HT export kWh{star}", f"HT net kWh{star}", f"Site load kWh{star}"]
        low = set(DAILY.loc[very_low_mask(DAILY["inverter_kwh"].dropna()).reindex(DAILY.index, fill_value=False), "date"])
        multi_year = first.year != last.year
        for r in DAILY.itertuples():
            inv_cell = ('<td class="b">—</td>' if customer else '<td class="i">No data</td>') if r.no_data \
                else f'<td class="b">{f0(r.inverter_kwh)}</td>'
            rows.append(
                f'<tr class="{"lowbg" if r.date in low else ""}"><td class="b">'
                f'{r.date:%d %b %Y}</td>' if multi_year else
                f'<tr class="{"lowbg" if r.date in low else ""}"><td class="b">{r.date:%d %b}</td>')
            rows[-1] += (f'<td>{r.day}</td><td class="wx">{escape(r.weather) or "—"}</td>'
                         f'<td>{"—" if pd.isna(r.temp_c) else f"{r.temp_c:.1f}"}</td>'
                         f'<td>{"—" if pd.isna(r.wind_ms) else f"{r.wind_ms:.1f}"}</td>{inv_cell}'
                         + "".join(f'<td class="{ic}">{f0(getattr(r, c))}</td>' for c in meters) + "</tr>")
        span = 5
    label = f"Total {MONTH_NAME}" if (month and not filtered) else f"Total {_span(first, last)}"
    rows.append(f'<tr class="total"><td colspan="{span}">{label}</td><td>{f0(DAILY["inverter_kwh"].sum(min_count=1))}</td>'
                + "".join(f"<td>{f0(DAILY[c].sum(min_count=1))}</td>" for c in meters) + "</tr>")
    st.markdown(f'<div class="ae-scroll"><table class="ae-t{" ae-grey" if customer else ""}"><tr>'
                + "".join(f'<th class="wx">{c}</th>' if c == "Weather" else f"<th>{c}</th>" for c in cols)
                + "</tr>" + "".join(rows) + "</table></div>", unsafe_allow_html=True)
    ht_import_calc()
    if customer:                       # customers download this table only (staff get the full report below)
        names = [c.rstrip("*") for c in cols]
        if GROUP == "Month":
            x = m[["label", "days", "inverter_kwh", *meters]].set_axis(names, axis=1)
            x = _with_total(x, names[0], names[2:])
        else:
            x = _day_cols(DAILY, ["weather", "temp_c", "wind_ms", "inverter_kwh", *meters]).set_axis(names, axis=1)
            x = _with_total(x, names[0], names[5:])
        excel_download(x, "Consolidated view", "consolidated_view")
        return
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        DAILY.drop(columns=["estimated"], errors="ignore").to_excel(xw, sheet_name="Daily statement", index=False)
        inv.drop(columns=["is_outage"], errors="ignore").to_excel(xw, sheet_name="Inverters", index=False)
        inverter_totals(inv).drop(columns="zero_days").to_excel(xw, sheet_name="Inverter totals", index=False)
        if BILL:
            pd.DataFrame([BILL]).to_excel(xw, sheet_name="HT bill", index=False)
    fname = f"srtpv_report_{month or f'{first:%Y%m%d}-{last:%Y%m%d}'}{'_filtered' if filtered else ''}.xlsx"
    st.download_button("Download this report's data (Excel)", buf.getvalue(), file_name=fname,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def observations(can_edit_notes: bool) -> None:
    n_est = int(sum(inv[f"proj_{p}"].sum() for p in INVERTER_IDS if f"proj_{p}" in inv)) if IS_PROJ else 0
    items = followups(MONTH_NAME or "Period", BILL, GRID, inverter_issues(inv, "month" if month else "period", SEL),
                      NODATA, n_est)
    note = db.load_note(month) if month else ""
    if note:
        items.append(("Engineer's note", note))
    icons = ["pf", "bolt", "warn", "search", "calc", "panel"]
    html = "".join(f'<div class="ae-oi">{ico(icons[min(k, len(icons) - 1)], "teal")}<div><b>{escape(t)}</b>'
                   f'<div>{escape(x)}</div></div></div>' for k, (t, x) in enumerate(items)) or \
        '<div style="color:#C9D2DC">No follow-up items this period.</div>'
    st.markdown(f'<div class="ae-obs"><h2>Observations and follow-up</h2>{html}</div>', unsafe_allow_html=True)
    if can_edit_notes and month:
        new = st.text_area("Engineer's note for this month (shown above)", value=note, height=90)
        if st.button("Save note"):
            db.save_note(month, new)
            st.success("Note saved")
            st.rerun()


def footer() -> None:
    period = f"{MONTH_NAME} {P_START:%Y}" if month else _span(first, last)
    word = f'<img src="data:image/png;base64,{LOGO_WORD}" height="20">' if LOGO_WORD else BRAND
    st.markdown(f'<div class="ae-foot"><span>{PLANT_SHORT} · {period} · {CONFIDENTIAL}</span>{word}</div>',
                unsafe_allow_html=True)


def more_analysis() -> None:
    with st.expander("More analysis: productivity, power factor, AC loss and meter readings"):
        productivity_section()
        eb_pf_section()
        if not ALL_SEL:
            st.info("AC loss compares all inverters with the LT meter; select all inverters to see it.")
        elif BILL_BASED:
            st.info("AC loss per reading period needs daily LT meter readings; this month's LT figures come from the bill.")
        else:
            ac_loss_section()
            meter_readings()


# =====================================================================
# Extra analysis (kept from the earlier dashboard)
# =====================================================================
def cards(items: list[tuple[str, str, str]]) -> None:
    for col, (label, value, note) in zip(st.columns(len(items)), items):
        with col.container(border=True):
            st.metric(label, value)
            st.caption(note)


def pf_text() -> str:
    if GRID["pf_avg"] is None:
        return "not recorded"
    return f"{GRID['pf_avg']:.3f} avg · {GRID['pf_min']:.3f} min"


def actual_vs_projected() -> None:
    """O&M Manager only: both data sets side by side for the days in view."""
    raw = RAW_INV[RAW_INV["date"].isin(inv["date"])].reset_index(drop=True)
    if raw.empty:
        return
    proj, replaced = project(raw, SHARES)
    a, pj = raw[SEL].sum(), proj[SEL].sum()
    n = replaced[SEL].sum()
    tbl = pd.DataFrame({"Inverter": SEL, "Location": [INVERTERS[p] for p in SEL],
                        "Actual kWh": a.values, "Projected kWh": pj.values,
                        "Difference kWh": (pj - a).values, "Readings projected": n.values})
    tbl.loc[len(tbl)] = ["Total", "", a.sum(), pj.sum(), pj.sum() - a.sum(), int(n.sum())]
    chronic = {m: [p for p in ps if p in SEL] for m, ps in chronic_inverters(raw).items()}
    parts = [f"{pd.Timestamp(m + '-01'):%B}: {', '.join(ps)} faulty on most days, so each day "
             f"{'it gets' if len(ps) == 1 else 'each gets'} {share_for(m, SHARES):.0%} of the day's actual total and "
             f"every reading is scaled to {1 - share_for(m, SHARES) * len(ps):.0%}." for m, ps in chronic.items() if ps]
    how = (" ".join(parts) + " ") if parts else "No inverter is faulty on most days, so projected equals actual. "
    h2("Actual vs projected", how + "Day and month totals are unchanged. Customers see the projected split.")
    rows = "".join(
        f'<tr class="{"total" if r["Inverter"] == "Total" else ""}"><td class="b">{r["Inverter"]}</td>'
        f'<td>{escape(r["Location"])}</td><td>{r["Actual kWh"]:,.2f}</td><td>{r["Projected kWh"]:,.2f}</td>'
        f'<td>{r["Difference kWh"]:+,.2f}</td><td>{int(r["Readings projected"])}</td></tr>'
        for _, r in tbl.iterrows())
    st.markdown('<div class="ae-tw"><table class="ae-t"><tr><th>Inverter</th><th>Location</th><th>Actual kWh</th>'
                '<th>Projected kWh</th><th>Difference</th><th>Days with share</th></tr>' + rows + "</table></div>",
                unsafe_allow_html=True)
    changed = [(d, p, raw.at[i, p], proj.at[i, p]) for i, d in enumerate(raw["date"]) for p in SEL
               if replaced.at[i, p]]
    with st.expander(f"Every day with a share ({len(changed)})"):
        st.dataframe(pd.DataFrame([{"Date": f"{d:%d %b %Y}", "Inverter": p,
                                    "Actual kWh": None if pd.isna(x) else round(x, 2), "Projected kWh": round(y, 2)}
                                   for d, p, x, y in changed]), hide_index=True, use_container_width=True)


# ---------------- customer portal views ----------------
def lt_view() -> None:
    h2("LT meter, date-wise with weather" if GROUP == "Day" else "LT meter, month-wise")
    st.plotly_chart(charts.lt_daily_chart(DAILY), use_container_width=True)
    f0 = lambda v: "—" if pd.isna(v) else f"{v:,.0f}"
    if GROUP == "Month":
        m = _month_rows(DAILY, ["lt_kwh"])
        head = "<th>Month</th><th>Days</th><th>LT meter kWh</th>"
        rows = "".join(f'<tr><td class="b">{r["label"]}</td><td>{int(r["days"])}</td><td class="b">{f0(r["lt_kwh"])}</td></tr>'
                       for _, r in m.iterrows())
        span = 2
    else:
        head = '<th>Date</th><th>Day</th><th class="wx">Weather</th><th>Temp °C</th><th>Wind m/s</th><th>LT meter kWh</th>'
        rows = "".join(
            f'<tr><td class="b">{r.date:%d %b}</td><td>{r.day}</td><td class="wx">{escape(r.weather) or "—"}</td>'
            f'<td>{"—" if pd.isna(r.temp_c) else f"{r.temp_c:.1f}"}</td>'
            f'<td>{"—" if pd.isna(r.wind_ms) else f"{r.wind_ms:.1f}"}</td><td class="b">{f0(r.lt_kwh)}</td></tr>'
            for r in DAILY.itertuples())
        span = 5
    rows += f'<tr class="total"><td colspan="{span}">Total</td><td>{f0(DAILY["lt_kwh"].sum(min_count=1))}</td></tr>'
    st.markdown(f'<div class="ae-scroll"><table class="ae-t"><tr>{head}</tr>{rows}</table></div>', unsafe_allow_html=True)
    if GROUP == "Month":
        x = _month_rows(DAILY, ["lt_kwh"])[["label", "days", "lt_kwh"]].set_axis(["Month", "Days", "LT meter kWh"], axis=1)
        x = _with_total(x, "Month", ["LT meter kWh"])
    else:
        x = _day_cols(DAILY, ["weather", "temp_c", "wind_ms", "lt_kwh"]).set_axis(
            ["Date", "Day", "Weather", "Temp °C", "Wind m/s", "LT meter kWh"], axis=1)
        x = _with_total(x, "Date", ["LT meter kWh"])
    excel_download(x, "LT meter view", "lt_meter_view")


def ht_view() -> None:
    grid_exchange()
    f0 = lambda v: "—" if pd.isna(v) else f"{v:,.0f}"
    cols3 = ("ht_import_kwh", "ht_export_kwh", "ht_net_kwh")
    if GROUP == "Month":
        m = _month_rows(DAILY, list(cols3))
        head = "<th>Month</th><th>Days</th>"
        rows = "".join(f'<tr><td class="b">{r["label"]}</td><td>{int(r["days"])}</td>'
                       + "".join(f"<td>{f0(r[c])}</td>" for c in cols3) + "</tr>" for _, r in m.iterrows())
    else:
        head = "<th>Date</th><th>Day</th>"
        rows = "".join(f'<tr><td class="b">{r.date:%d %b}</td><td>{r.day}</td>'
                       + "".join(f"<td>{f0(getattr(r, c))}</td>" for c in cols3) + "</tr>" for r in DAILY.itertuples())
    rows += ('<tr class="total"><td colspan="2">Total</td>'
             + "".join(f"<td>{f0(DAILY[c].sum(min_count=1))}</td>" for c in cols3) + "</tr>")
    st.markdown(f'<div class="ae-scroll" style="margin-top:12px"><table class="ae-t"><tr>{head}'
                '<th>Import kWh</th><th>Export kWh</th><th>Net import kWh</th></tr>' + rows + "</table></div>",
                unsafe_allow_html=True)
    ht_import_calc()
    names = ["Import kWh", "Export kWh", "Net import kWh"]
    if GROUP == "Month":
        x = _month_rows(DAILY, list(cols3))[["label", "days", *cols3]].set_axis(["Month", "Days", *names], axis=1)
        x = _with_total(x, "Month", names)
    else:
        x = _day_cols(DAILY, list(cols3)).set_axis(["Date", "Day", *names], axis=1)
        x = _with_total(x, "Date", names)
    excel_download(x, "HT meter view", "ht_meter_view")


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


# =====================================================================
# One dashboard for every role. Only the inverter values differ by view.
# =====================================================================
def dashboard(can_edit_notes: bool, show_grafana: bool) -> None:
    page_logo()
    if IS_CUSTOMER:                     # customer portal: three sections only
        customer_title()
        at_a_glance(customer=True)                        # overall dashboard
        t_inv, t_lt, t_ht, t_all = st.tabs(["Inverter view", "LT meter view", "HT meter view", "Consolidated view"])
        with t_inv:
            inverter_readings(with_filter=True)
        with t_lt:
            lt_view()
        with t_ht:
            ht_view()
        with t_all:
            pv_system_comparison()
            consolidated_inverter_note()
            inverter_vs_lt_bars()
            statement(customer=True)
        return
    if show_grafana and GRAFANA_URL:
        st.link_button("Open live monitoring in Grafana", GRAFANA_URL)
    cover()
    at_a_glance()
    if IS_ADMIN and VIEWS != ["Actuals"]:
        actual_vs_projected()
    bill_slide()
    daily_generation()
    inverter_vs_lt()
    inverter_readings()
    month_total()
    grid_exchange()
    statement()
    observations(can_edit_notes)
    footer()
    more_analysis()


if IS_ADMIN:                                   # Asase O&M Manager
    tab_report, tab_import, tab_users = st.tabs(["Dashboard", "Import data", "User access"])
    with tab_report:
        dashboard(can_edit_notes=True, show_grafana=True)
    with tab_import:
        import_page()
    with tab_users:
        users_page()
elif IS_SITE:                                  # Asase O&M Executive
    tab_report, tab_import = st.tabs(["Dashboard", "Import data"])
    with tab_report:
        dashboard(can_edit_notes=False, show_grafana=True)
    with tab_import:
        import_page()
else:                                          # Customer
    dashboard(can_edit_notes=False, show_grafana=False)
