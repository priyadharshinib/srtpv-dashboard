# SRTPV plant monthly report (Streamlit)

Reproduces the October 2026 generation & grid report: KPI cards, daily generation
with weather, inverter-wise table with flags, inverter totals, HT import/export,
consolidated daily statement and auto-generated observations.

## Setup (Windows, VS Code terminal)

    cd solar-dashboard
    py -3.12 -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    pytest
    streamlit run app.py

Click **Load October 2026 sample** in the sidebar to see the report immediately.

## Input sheets (.xlsx or .csv, first sheet, row 1 = headers)

| Sheet | Required columns | Optional |
|---|---|---|
| Inverter | date, PV1 … PV9 | Total (checked), temp_c, weather, wind_ms |
| LT meter | from_date, to_date, opening, closing | kwh (checked against units × 300), period, note |
| HT meter | date, import_kwh, export_kwh (or import_units, export_units × 6000) | power_factor (0–1, also accepts pf), note |

Dates can be 2026-10-01, 01-10-2026 or Excel dates. Re-importing a date replaces it.

## Layout

    app.py               Streamlit page
    core/config.py       Plant settings, inverter names, MFs, flag thresholds
    core/importer.py     Read + validate Excel/CSV
    core/calculations.py KPIs, totals, flags, daily statement (pure pandas)
    core/observations.py Rule-based observations and headline text
    core/charts.py       Plotly figures
    core/db.py           SQLAlchemy models, save/load (SQLite → Postgres via DATABASE_URL)
    tests/test_core.py   Pytest checks against the October numbers

## Going online (Supabase + Streamlit Community Cloud + Grafana Cloud)

    .streamlit/secrets.toml.example  Template for DATABASE_URL and GRAFANA_URL
    grafana/01_grafana_reader.sql    Read-only database login for Grafana (run in Supabase)
    grafana/02_dashboard.json        Import into Grafana: Dashboards > New > Import
    grafana/03_alert_query.sql       Query for the weak-inverter alert (ratio below 0.5)

Online, tables live in the `solar` schema (not `public`), so Supabase's public
Data API can't read them.

## Logins and roles

Logins use Supabase Auth (email + password). Roles are kept in `solar.app_users`
(internal key → name in the app):

| Key | Name | Dashboard | Import | Users |
|---|---|---|---|---|
| admin | Asase O&M Manager | Actuals **or** Avg (sidebar switch) | ✅ | ✅ |
| site_manager | Asase O&M Executive | Actuals | ✅ | — |
| customer | Customer | Avg only | — | — |

All roles see the same dashboard. Only PV1–PV9 values differ between views.

## Report-style dashboard

The dashboard follows the asase.energy monthly report, section by section: cover,
at a glance, HT bill, daily inverter generation with weather tiles, inverter vs LT
meter, inverter-wise readings (two halves), month total by inverter, grid exchange,
consolidated daily statement, observations and follow-up. Extra analysis
(productivity, power factor, AC loss, meter readings) is under "More analysis".
Code: the report section of `app.py`, `core/report.py`, report charts in `core/charts.py`.

## Missing data and estimates (`core/estimation.py`)

- **Missing inverter reading** (blank or n/a): filled so the day matches the sheet's
  Total; with no Total, from that inverter's usual share. Marked "e" in the table
  (Actuals View only) and listed in the import message / bot reply.
- **Day with no inverter data at all**: shown as "No data"; the chart shows an
  estimate (grey) from the LT meter.
- **Bill totals entered** (LT units, HT import, HT export): the bill is the month's
  LT/HT figure. Daily values are spread as in the report: a no-data day gets LT ÷ days
  in month, the rest follows inverter output; site load by day type (weekday 1.0,
  Sat 0.8471, Sun 0.7097 in `core/config.py`); HT import = site load − LT + export;
  export from the daily HT sheet. Shown in italic with *.

## Importing a month in parts

Import data → choose **Data month**, then upload. "Already imported" shows which days
each sheet has so far. Days already in the database are skipped (or replaced, if you
choose that), dates outside the chosen month are refused, and an LT batch must continue
from the last saved closing reading. Code: `core/batches.py`.

## Customer portal

Customers see the logo, title, "at a glance" (without outage notes) and the
consolidated daily statement only — no footer, estimate markers or analysis wording.

## Monthly HT bill

Import data → **Monthly HT bill**: net payable, PF penalty, one other charge (e.g. FY25
true-up), average PF, maximum demand with date/time, contract demand and the export
settlement note. The dashboard then shows the "HT bill and demand" block, with an
automatic headline and a "Why the demand peaked" note built from that day's solar output.

## Avg View (temporary)

In the Avg View each inverter shows that day's plant total divided by 9, so the
plant total and every other figure stay identical. Code: `core/views.py` and the
lines marked `AVG-VIEW` in `app.py`.

To retire it: set the secret `AVG_VIEW_ENABLED = "false"` (everyone sees Actuals,
no deploy needed), then later delete `core/views.py`, the `AVG-VIEW` lines in
`app.py` and the `AVG-VIEW` tests in `tests/test_core.py`.

## Filters (sidebar)

Month or any date range · weather group · productivity band (kWh per kWp per day,
bands in core/config.py) · weekday/weekend · which inverters to show.
Outage days are judged on the whole loaded period, so filtering doesn't change them.

## Data bot (automatic import by email + automatic weather)

    bot/email_bot.py              Reads unread emails, imports attached sheets, replies with a report
    bot/weather.py                Fills missing weather from Open-Meteo (free, no key)
    bot/detect.py                 Tells inverter / LT / HT sheets apart by their columns
    .github/workflows/data-bot.yml  Runs both every 3 hours on GitHub Actions

GitHub repo > Settings > Secrets and variables > Actions > New repository secret:
DATABASE_URL, BOT_IMAP_HOST, BOT_SMTP_HOST, BOT_EMAIL, BOT_PASSWORD,
ALLOWED_SENDERS (comma-separated), BOT_NOTIFY (optional, copied on replies).

Every import (app, email bot, weather bot) is recorded in `solar.import_log`
and shown under Import data > Import history.
