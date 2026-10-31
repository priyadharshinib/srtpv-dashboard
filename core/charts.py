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


GREY = "#9D9D9D"


def ac_loss_chart(losses: pd.DataFrame) -> go.Figure:
    """Loss % per LT period, coloured by status, with the expected band shaded."""
    from core.config import AC_LOSS_EXPECTED

    colour = {"Normal": TEAL, "Below expected": BLUE, "High loss: check AC side": ORANGE,
              "LT higher: inverter data gap": GREY}
    fig = go.Figure(go.Bar(
        x=losses["period"], y=losses["loss_pct"] * 100,
        marker_color=[colour.get(s, BLUE) for s in losses["status"]],
        customdata=losses[["inverter_kwh", "lt_kwh", "loss_kwh", "status"]],
        hovertemplate=("Inverters %{customdata[0]:,.0f} kWh<br>LT meter %{customdata[1]:,.0f} kWh<br>"
                       "Loss %{customdata[2]:,.0f} kWh (%{y:.1f}%)<br>%{customdata[3]}<extra></extra>"),
    ))
    lo, hi = AC_LOSS_EXPECTED
    fig.add_hrect(y0=lo * 100, y1=hi * 100, fillcolor=TEAL, opacity=0.12, line_width=0,
                  annotation_text="expected", annotation_position="top left")
    fig = _layout(fig, 320)
    fig.update_yaxes(title="Loss %", tickformat=".0f", zeroline=True, zerolinecolor="rgba(128,128,128,0.6)")
    fig.update_layout(hovermode="closest")
    return fig


def power_factor_chart(ht: pd.DataFrame) -> go.Figure:
    from core.config import PF_LIMIT

    d = ht.dropna(subset=["power_factor"])
    fig = go.Figure(go.Scatter(
        x=d["date"].dt.strftime("%d %b"), y=d["power_factor"], mode="lines+markers", name="Power factor",
        line=dict(color=BLUE, width=2),
        marker=dict(size=7, color=[RED if v < PF_LIMIT else BLUE for v in d["power_factor"]]),
        hovertemplate="PF %{y:.3f}<extra></extra>",
    ))
    fig.add_hline(y=PF_LIMIT, line_dash="dash", line_color=RED,
                  annotation_text=f"limit {PF_LIMIT:.2f}", annotation_position="bottom right")
    fig = _layout(fig, 280)
    fig.update_yaxes(tickformat=".2f", range=[min(0.8, float(d["power_factor"].min()) - 0.02), 1.0])
    return fig


def customer_daily(inv: pd.DataFrame) -> go.Figure:
    """Average output per inverter per day, with weather under each date."""
    from core.config import INVERTER_IDS

    avg = inv["Total"] / len(INVERTER_IDS)
    labels = [f"{d:%d}<br>{weather_icon(w)}" for d, w in zip(inv["date"], inv["weather"])]
    fig = go.Figure(go.Bar(
        x=labels, y=avg, marker_color=BLUE, name="Average per inverter",
        customdata=inv[["weather"]],
        hovertemplate="%{y:,.0f} kWh per inverter · %{customdata[0]}<extra></extra>",
    ))
    fig.add_hline(y=float(avg.mean()), line_dash="dot", line_color=ORANGE,
                  annotation_text=f"period average {avg.mean():,.0f} kWh", annotation_position="top left")
    return _layout(fig, 340)


# ---------------------------------------------------------------------
# Report-style charts (asase.energy monthly report colours)
# ---------------------------------------------------------------------
R_AMBER, R_RED, R_GREY, R_TEAL = "#E5A01A", "#C0392B", "#D5DBE3", "#2A9D8F"
R_NAVY, R_CORAL, R_INK, R_GRID = "#3D5A80", "#E07B5F", "#5B6B7B", "rgba(91,107,123,0.18)"


def _r_layout(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=30, b=10), plot_bgcolor="rgba(0,0,0,0)",
                      paper_bgcolor="rgba(0,0,0,0)", font=dict(color=R_INK, size=12), bargap=0.25,
                      legend=dict(orientation="h", y=1.12, x=1, xanchor="right"), hovermode="x unified")
    fig.update_yaxes(gridcolor=R_GRID, tickformat=",.0f", zeroline=False)
    fig.update_xaxes(showgrid=False, type="category")
    return fig


def report_daily_chart(daily: pd.DataFrame) -> go.Figure:
    from core.report import very_low_mask

    x = daily["date"].dt.strftime("%d")
    actual = daily["inverter_kwh"]
    low = very_low_mask(actual.dropna()).reindex(daily.index, fill_value=False)
    fig = go.Figure()
    fig.add_bar(x=x, y=daily["inverter_est"].where(daily["no_data"]), name="No data, estimated (kWh)",
                marker_color=R_GREY, hovertemplate="%{y:,.0f} kWh (estimated)<extra></extra>")
    fig.add_bar(x=x, y=actual.where(low), name="Very low day (kWh)", marker_color=R_RED,
                hovertemplate="%{y:,.0f} kWh<extra></extra>")
    fig.add_bar(x=x, y=actual.where(~low), name="Inverter total (kWh)", marker_color=R_AMBER,
                hovertemplate="%{y:,.0f} kWh<extra></extra>")
    fig.update_layout(barmode="overlay")
    return _r_layout(fig, 360)


def inverter_vs_lt_chart(daily: pd.DataFrame, lt_estimated: bool, inverter_label: str = "Inverter (actual)") -> go.Figure:
    x = daily["date"].dt.strftime("%d")
    fig = go.Figure()
    fig.add_scatter(x=x, y=daily["inverter_kwh"], name=inverter_label, mode="lines+markers",
                    line=dict(color=R_AMBER, width=2.5), marker=dict(size=7), connectgaps=False)
    fig.add_scatter(x=x, y=daily["lt_kwh"], name="LT meter (daily estimate)" if lt_estimated else "LT meter",
                    mode="lines+markers", line=dict(color=R_TEAL, width=2.5), marker=dict(size=7))
    return _r_layout(fig, 400)


def month_total_chart(totals: pd.DataFrame) -> go.Figure:
    from core.config import INVERTER_SHORT

    t = totals.iloc[::-1]
    fig = go.Figure(go.Bar(
        x=t["kwh"], y=[f"{p} · {INVERTER_SHORT.get(p, '')}" for p in t["inverter"]], orientation="h",
        marker_color=R_AMBER, text=t["kwh"].map("{:,.0f}".format), textposition="inside",
        insidetextanchor="end", textfont=dict(color="white"), hovertemplate="%{x:,.0f} kWh<extra></extra>"))
    fig = _r_layout(fig, 420)
    fig.update_xaxes(type="linear", showgrid=False, showticklabels=False)
    fig.update_yaxes(type="category", gridcolor="rgba(0,0,0,0)")
    fig.update_layout(hovermode="closest", bargap=0.3)
    return fig


def grid_exchange_report(daily: pd.DataFrame) -> go.Figure:
    x = daily["date"].dt.strftime("%d")
    exp = daily["ht_export_kwh"].fillna(0)
    weekends_only = bool(exp.gt(0).any()) and bool((daily.loc[exp > 0, "date"].dt.dayofweek >= 5).all())
    fig = make_subplots(rows=2, cols=1, shared_xaxes=False, row_heights=[0.68, 0.32], vertical_spacing=0.16,
                        subplot_titles=("Daily import (kWh)",
                                        "Daily export (kWh)" + (", weekends only" if weekends_only else "")))
    fig.add_bar(x=x, y=daily["ht_import_kwh"], marker_color=R_NAVY, name="Import",
                hovertemplate="%{y:,.0f} kWh<extra>Import</extra>", row=1, col=1)
    fig.add_bar(x=x, y=exp.where(exp > 0), marker_color=R_CORAL, name="Export",
                text=exp.where(exp > 0).map(lambda v: "" if pd.isna(v) else f"{v:,.0f}"),
                textposition="outside", hovertemplate="%{y:,.0f} kWh<extra>Export</extra>", row=2, col=1)
    fig = _r_layout(fig, 430)
    fig.update_layout(showlegend=False, hovermode="closest")
    fig.update_annotations(font=dict(size=13, color=R_INK))
    fig.update_yaxes(range=[0, max(float(exp.max()) * 1.35, 1)], row=2, col=1)
    return fig


def lt_daily_chart(daily: pd.DataFrame) -> go.Figure:
    """LT meter kWh per day (customer LT view)."""
    x = daily["date"].dt.strftime("%d")
    fig = go.Figure(go.Bar(x=x, y=daily["lt_kwh"], marker_color=R_TEAL, name="LT meter (kWh)",
                           customdata=daily[["weather"]].fillna(""),
                           hovertemplate="%{y:,.0f} kWh · %{customdata[0]}<extra></extra>"))
    return _r_layout(fig, 340)


# ---------------------------------------------------------------------
# Consolidated view charts
# ---------------------------------------------------------------------
# Colours and stacking order follow the inverter portal's "PV system comparison"
# (bottom to top: PV7, PV8, PV9, PV6, PV5, PV4, PV3, PV1, PV2).
PV_COLOURS = {
    "PV1": "#6DBF4B", "PV2": "#6BAED0", "PV3": "#3F4A9E",
    "PV4": "#0FA58D", "PV5": "#00695F", "PV6": "#9C59A8",
    "PV7": "#F6C000", "PV8": "#E2431E", "PV9": "#B5174F",
}
PV_STACK_ORDER = ["PV7", "PV8", "PV9", "PV6", "PV5", "PV4", "PV3", "PV1", "PV2"]


def pv_comparison_chart(inv: pd.DataFrame) -> go.Figure:
    """Daily energy stacked by PV system. Days without a row show as empty slots."""
    from core.config import PV_SYSTEM_NAMES

    full = pd.date_range(inv["date"].min(), inv["date"].max())
    d = inv.set_index("date").reindex(full)
    multi_month = d.index.to_period("M").nunique() > 1
    x = d.index.strftime("%d %b" if multi_month else "%d")
    fig = go.Figure()
    for p in PV_STACK_ORDER:
        fig.add_bar(x=x, y=d[p], name=PV_SYSTEM_NAMES[p], marker_color=PV_COLOURS[p],
                    hovertemplate="%{y:,.0f} kWh<extra>" + PV_SYSTEM_NAMES[p] + "</extra>")
    fig.update_layout(barmode="stack")
    fig = _r_layout(fig, 460)
    fig.update_layout(showlegend=False, bargap=0.18)
    fig.update_yaxes(title_text="Energy [kWh]")
    return fig


def inverter_vs_lt_bar_chart(daily: pd.DataFrame, lt_estimated: bool, inverter_label: str = "Inverter") -> go.Figure:
    """Side-by-side daily bars: inverter total and LT meter."""
    x = daily["date"].dt.strftime("%d")
    fig = go.Figure()
    fig.add_bar(x=x, y=daily["inverter_kwh"], name=f"{inverter_label} (kWh)", marker_color=R_AMBER,
                hovertemplate="%{y:,.0f} kWh<extra>" + inverter_label + "</extra>")
    fig.add_bar(x=x, y=daily["lt_kwh"], name="LT meter, daily estimate (kWh)" if lt_estimated else "LT meter (kWh)",
                marker_color=R_TEAL, hovertemplate="%{y:,.0f} kWh<extra>LT meter</extra>")
    fig.update_layout(barmode="group")
    fig = _r_layout(fig, 400)
    fig.update_layout(bargap=0.25, bargroupgap=0.05)
    fig.update_yaxes(title_text="Energy [kWh]")
    return fig
