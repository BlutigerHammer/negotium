"""
app.py — Negotium - Investment Tracker UI (Streamlit)

Run: streamlit run src/app.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

import ledger_core
from ui.dashboard import render_dashboard
from ui.helpers import detect_currency
from ui.runtime import init_runtime
from ui.sidebar import render_sidebar
from ui.styles import (
    build_app_styles,
    build_holdings_styles,
    build_late_theme_override,
    build_metric_card_styles,
    build_theme_veil,
    build_toggle_button_styles,
    build_trade_dialog_styles,
)

st.set_page_config(
    page_title="Negotium",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

cfg, storage, _theme_name, T, today = init_runtime()

# Every theme-dependent stylesheet in one early injection, so a theme switch
# recolors the whole app with the first painted frame instead of section by
# section as each component injects its own CSS mid-render. The per-component
# injections stay in place (identical rules — no visual effect) so components
# remain styled when rendered outside the normal page flow (e.g. dialogs).
st.markdown(
    build_app_styles(T)
    + build_late_theme_override(T)
    + build_metric_card_styles(T)
    + build_toggle_button_styles(T)
    + build_holdings_styles(T)
    + build_trade_dialog_styles(T),
    unsafe_allow_html=True,
)

# Cover the staggered re-render right after a theme switch (flag set by the
# sidebar toggle) so the change reads as one atomic step.
_fade_ts = st.session_state.pop("theme_fade", None)
if _fade_ts is not None and (time.time() - _fade_ts) < 3:
    st.markdown(build_theme_veil(T), unsafe_allow_html=True)

data_start_date = ledger_core.first_portfolio_date() or today
base_ccy = render_sidebar(cfg, storage, T, today, data_start_date, detect_currency)
render_dashboard(cfg, storage, T, today, data_start_date, base_ccy)
