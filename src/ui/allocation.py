"""
allocation.py — Allocation Breakdown section

Aggregates current holdings (latest["assets"]) by sector, geography,
asset class and currency, rendering each as a donut chart.
"""
from __future__ import annotations

import colorsys

import plotly.graph_objects as go
import streamlit as st
from bonds import retail_bond_metadata
from ui.colors import ACCENT


def _accent_palette(n: int, accent_hex: str, is_light: bool) -> list[str]:
    """Build a coherent, theme-family palette by rotating the accent's hue.

    Lightness is kept low so the white inside labels stay readable on every
    slice (including yellow/green hues) in both themes.
    """
    h = accent_hex.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    base_h, _base_l, base_s = colorsys.rgb_to_hls(r, g, b)
    out = []
    for i in range(n):
        nh = (base_h + i * 0.12) % 1.0
        nr, ng, nb = colorsys.hls_to_rgb(nh, 0.42, base_s)
        out.append("#%02x%02x%02x" % (int(nr * 255), int(ng * 255), int(nb * 255)))
    return out


def render_allocation_breakdown(latest_assets, base_ccy: str, today, T: dict, get_ticker_meta) -> None:
    if not latest_assets:
        return

    total = sum(a["value_base"] for a in latest_assets) or 1.0

    cats = {
        "Sector": {},
        "Geography": {},
        "Asset class": {},
        "Currency": {},
    }

    for a in latest_assets:
        ticker = a["ticker"]
        val = a["value_base"]
        meta = retail_bond_metadata(ticker) or get_ticker_meta(ticker)
        sector = meta.get("sector") or "Unknown"
        country = meta.get("country") or "Unknown"
        aclass = meta.get("asset_class") or "Unknown"
        ccy = a.get("currency") or "—"
        cats["Sector"][sector] = cats["Sector"].get(sector, 0.0) + val
        cats["Geography"][country] = cats["Geography"].get(country, 0.0) + val
        cats["Asset class"][aclass] = cats["Asset class"].get(aclass, 0.0) + val
        cats["Currency"][ccy] = cats["Currency"].get(ccy, 0.0) + val

    tabs = st.tabs(["Sector", "Geography", "Asset class", "Currency"])
    for tab, key in zip(tabs, ["Sector", "Geography", "Asset class", "Currency"]):
        with tab:
            _render_donut(cats[key], total, T, chart_key=f"alloc_{key}")


def _render_donut(data: dict, total: float, T: dict, chart_key: str = "alloc") -> None:
    if not data:
        st.caption("No data.")
        return

    items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)
    labels = [k for k, _ in items]
    values = [v for _, v in items]

    is_light = st.session_state.get("theme", "dark") == "light"
    accent = T.get("accent", ACCENT)
    slice_colors = _accent_palette(len(labels), accent, is_light)

    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.45,
                sort=False,
                textinfo="label+percent",
                textposition="inside",
                insidetextorientation="radial",
                textfont=dict(size=24, color="#ffffff"),
                marker=dict(colors=slice_colors),
                domain=dict(x=[0.0, 0.62], y=[0.0, 1.0]),
            )
        ]
    )
    txt = "#1F2328" if is_light else "#E6EDF3"

    fig.update_layout(
        height=900,
        margin=dict(t=10, b=10, l=10, r=10),
        paper_bgcolor=T["chart_bg"],
        plot_bgcolor=T["chart_bg"],
        font=dict(color=txt, size=22),
        showlegend=True,
        legend=dict(
            orientation="v",
            x=0.6,
            y=0.5,
            xanchor="left",
            yanchor="middle",
            font=dict(size=28, color=txt),
            bgcolor="rgba(0,0,0,0)",
        ),
    )
    st.plotly_chart(fig, width='stretch', key=chart_key)
