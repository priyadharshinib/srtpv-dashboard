"""Fill in missing daily weather (temperature, sky, wind) from Open-Meteo (free, no API key).

Only days that have inverter readings but no weather are filled; weather typed
in the Excel sheets is never overwritten. Values are modelled for the plant's
coordinates (core/config.py), not measured on site.
Run by hand:  python -m bot.weather
"""
from __future__ import annotations

import datetime as dt
import sys

import pandas as pd
import requests

from core import db
from core.config import PLANT_LAT, PLANT_LON, PLANT_TIMEZONE

# WMO weather codes -> the weather words used in the sheets (grouped by core/filters.weather_group)
WMO = {
    0: "Sunny / Clear", 1: "Sunny / Hazy", 2: "Partly Cloudy", 3: "Cloudy",
    45: "Cloudy / Fog", 48: "Cloudy / Fog",
    51: "Overcast / Drizzle", 53: "Overcast / Drizzle", 55: "Overcast / Drizzle",
    56: "Overcast / Drizzle", 57: "Overcast / Drizzle",
    61: "Light Rain", 63: "Moderate Rain", 65: "Heavy Rain", 66: "Light Rain", 67: "Heavy Rain",
    80: "Rain Showers", 81: "Rain Showers", 82: "Heavy Rain",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}
FORECAST = "https://api.open-meteo.com/v1/forecast"          # recent days (up to ~3 months back)
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"     # older days (complete after ~5 days)


def parse(payload: dict) -> pd.DataFrame:
    d = payload["daily"]
    return pd.DataFrame({
        "date": pd.to_datetime(d["time"]),
        "temp_c": pd.Series(d["temperature_2m_mean"], dtype="float").round(1),
        "weather": [WMO.get(int(c), "Cloudy") if c is not None else "" for c in d["weather_code"]],
        "wind_ms": pd.Series(d["wind_speed_10m_max"], dtype="float").round(1),
    })


def fetch(start: dt.date, end: dt.date) -> pd.DataFrame:
    params = {
        "latitude": PLANT_LAT, "longitude": PLANT_LON, "timezone": PLANT_TIMEZONE,
        "start_date": start.isoformat(), "end_date": end.isoformat(), "wind_speed_unit": "ms",
        "daily": "weather_code,temperature_2m_mean,wind_speed_10m_max",
    }
    recent = (dt.date.today() - start).days <= 80
    url = FORECAST if recent else ARCHIVE
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200 and recent:          # fall back to the archive
        r = requests.get(ARCHIVE, params=params, timeout=30)
    r.raise_for_status()
    df = parse(r.json())
    return df[df["weather"] != ""]


def run(days_back: int = 120) -> int:
    db.init_db()
    missing = db.dates_missing_weather(dt.date.today() - dt.timedelta(days=days_back))
    if not missing:
        print("Weather: nothing missing")
        return 0
    df = fetch(missing[0], missing[-1])
    df = df[df["date"].dt.date.isin(missing)]
    n = db.fill_weather(df)
    print(f"Weather: filled {n} of {len(missing)} missing day(s)")
    db.log_import("weather-bot", "open-meteo", "-", "Weather", "saved", n, "")
    return 0


if __name__ == "__main__":
    sys.exit(run())
