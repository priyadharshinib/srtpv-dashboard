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
| HT meter | date, import_kwh, export_kwh (or import_units, export_units × 6000) | note |

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
