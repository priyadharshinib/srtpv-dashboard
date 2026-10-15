"""Plotly figures for the dashboard."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from core.calculations import outage_mask, single_day_lt

BLUE = "#4C78A8"
ORANGE = "#F58518"
TEAL = "#54A24B"
PURPLE = "#B279A2"
RED = "#E45756"


def weather_icon(w: str) -> str:
    s = (w or "").lower()
    if any(k in s for k in ("rain", "thunder", "shower")):
        return "🌧"
    if any(k in s for k in ("sun", "clear", "bright")):
        return "🌤" if any(k in s for k in ("partly", "hazy", "cloud")) else "☀️"
    if any(k in s for k in ("partly", "hazy")):
        return "⛅"
    return "☁️"


def _layout(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
        legend=dict(orientation="h", y=1.08, x=0), plot_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.2)", tickformat=",.0f")
    return fig


def daily_generation(inv: pd.DataFrame, lt: pd.DataFrame) -> go.Figure:
    """Inverter total bars (red on outage days) + LT single-day dots, weather under the axis."""
    labels = [
        f"{d:%d}<br>{weather_icon(w)}<br>{t:.0f}°" if pd.notna(t) else f"{d:%d}<br>{weather_icon(w)}"
        for d, w, t in zip(inv["date"], inv["weather"], inv["temp_c"])
    ]
    colors = [RED if o else BLUE for o in outage_mask(inv)]
    fig = go.Figure()
    fig.add_bar(
        x=labels, y=inv["Total"], marker_color=colors, name="Inverter total",
        customdata=inv[["weather"]], hovertemplate="%{y:,.0f} kWh · %{customdata[0]}<extra>Inverter</extra>",
    )
    lt1 = single_day_lt(lt).set_index("date")["kwh"]
    lt_y = [lt1.get(d) for d in inv["date"]]
    fig.add_scatter(
        x=labels, y=lt_y, mode="markers", name="LT meter (single day)",
        marker=dict(size=10, color=ORANGE, line=dict(width=1.5, color="white")),
        hovertemplate="%{y:,.0f} kWh<extra>LT meter</extra>",
    )
    return _layout(fig, 360)


def inverter_totals_bar(totals: pd.DataFrame, highlight: list[str]) -> go.Figure:
    t = totals.iloc[::-1]
    fig = go.Figure(go.Bar(
        x=t["kwh"], y=t["inverter"] + " · " + t["location"], orientation="h",
        marker_color=[RED if p in highlight else BLUE for p in t["inverter"]],
        text=t["kwh"].map("{:,.0f}".format), textposition="outside",
        hovertemplate="%{x:,.0f} kWh<extra></extra>",
    ))
    fig = _layout(fig, 360)
    fig.update_layout(hovermode="closest")
    fig.update_xaxes(tickformat=",.0f")
    return fig


def grid_exchange(ht: pd.DataFrame) -> go.Figure:
    """Import on top, export below on its own smaller scale."""
    x = ht["date"].dt.strftime("%d")
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28], vertical_spacing=0.06)
    fig.add_bar(x=x, y=ht["import_kwh"], name="Import", marker_color=TEAL,
                customdata=ht[["net_import_kwh", "note"]],
                hovertemplate="Import %{y:,.0f} kWh · Net %{customdata[0]:,.0f} %{customdata[1]}<extra></extra>",
                row=1, col=1)
    fig.add_bar(x=x, y=ht["export_kwh"], name="Export", marker_color=PURPLE,
                hovertemplate="Export %{y:,.0f} kWh<extra></extra>", row=2, col=1)
    return _layout(fig, 360)
