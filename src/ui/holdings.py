from __future__ import annotations

import streamlit as st

from bonds import bond_value_from_holding, is_retail_bond, load_inflation
from ui.colors import ACCENT, NEGATIVE, RETURN_UP
from ui.styles import build_holdings_styles


@st.dialog("Polish retail bonds", width="large")
def _show_retail_bond_details(rows: list[dict], base_ccy: str) -> None:
    if rows:
        st.caption("Holdings are grouped by bond series; purchases are not shown separately.")
        for row in rows:
            with st.container(border=True):
                series, units, value = st.columns([2, 1, 2])
                with series:
                    st.markdown(f"**{row['ticker']}**")
                    if row["ticker"].startswith("EDO"):
                        st.caption("10-year inflation-linked")
                    else:
                        st.caption("3-year fixed-rate")
                with units:
                    st.metric("Units", f"{row['units']:,.4f}".rstrip("0").rstrip("."))
                with value:
                    amount = f"{row['value']:,.2f}".replace(",", " ")
                    if base_ccy == "PLN":
                        amount = f"{amount} PLN"
                    elif base_ccy == "EUR":
                        amount = f"€{amount}"
                    else:
                        amount = f"${amount}"
                    st.metric(f"Market value ({base_ccy})", amount)
    else:
        st.info("No retail bond positions to show.")


def render_holdings_table(T: dict[str, str], latest_assets, base_ccy: str, today, storage,
                         get_fx_rate, get_ticker_name, show_trade_dialog) -> None:
    if not latest_assets:
        return

    total_val = max(sum(a["value_base"] for a in latest_assets), 1.0)
    bal = storage.load_balance()

    rows = []
    bond_summary = {"shares": 0.0, "value": 0.0}
    avg_fx_cache: dict = {}
    ticker_names = {a["ticker"]: get_ticker_name(a["ticker"]) for a in latest_assets}
    bond_detail_rows = [
        {
            "ticker": asset["ticker"],
            "units": float(asset["amount"]),
            "value": float(asset["value_base"]),
        }
        for asset in latest_assets if is_retail_bond(asset["ticker"])
    ]
    for a in sorted(latest_assets, key=lambda x: x["value_base"], reverse=True):
        ticker = a["ticker"]
        shares = a["amount"]
        value = a["value_base"]
        if is_retail_bond(ticker):
            bond_summary["shares"] += shares
            bond_summary["value"] += value
            continue
        ticker_ccy = a.get("currency", "PLN")
        avg_raw = bal.get(ticker, {}).get("avg_price", 0.0)
        if ticker_ccy != base_ccy:
            avg_ccy_fx = get_fx_rate(ticker_ccy, base_ccy, today.isoformat(), avg_fx_cache, today.year)
        else:
            avg_ccy_fx = 1.0
        avg = avg_raw * avg_ccy_fx
        cost_basis = shares * avg
        ret_pct = ((value / cost_basis) - 1) * 100 if cost_basis else 0.0
        rows.append({
            "ticker": ticker,
            "name": ticker_names.get(ticker, ticker),
            "ccy": a.get("currency", "—"),
            "weight": value / total_val * 100,
            "shares": shares,
            "value": value,
            "ret_pct": ret_pct,
        })

    if bond_summary["shares"]:
        bond_cost_fx = (
            get_fx_rate("PLN", base_ccy, today.isoformat(), avg_fx_cache, today.year)
            if base_ccy != "PLN" else 1.0
        )
        bond_cost = bond_summary["shares"] * 100.0 * bond_cost_fx
        rows.append({
            "ticker": "BONDS",
            "name": "Retail bonds",
            "ccy": "PLN",
            "weight": bond_summary["value"] / total_val * 100,
            "shares": bond_summary["shares"],
            "value": bond_summary["value"],
            "ret_pct": (bond_summary["value"] / bond_cost - 1) * 100,
            "is_bond_group": True,
        })

    def _fmt_val(v: float) -> str:
        s = f"{v:,.1f}".replace(",", " ")
        return f"{SYM.get(base_ccy, '')}{s}" if base_ccy != "PLN" else f"{s} PLN"

    def _fmt_ret(p: float) -> str:
        return f"{p:+.1f}%"

    def _ret_color(p: float) -> str:
        return RETURN_UP if p >= 0 else NEGATIVE

    SYM = {"PLN": " PLN", "EUR": "€", "USD": "$"}

    st.markdown(build_holdings_styles(T), unsafe_allow_html=True)

    headers = st.columns([5, 1, 1, 1, 2, 1])
    for col, label in zip(headers, ["Ticker", "CCY", "Weight", "Shares", "Value", "Return %"]):
        with col:
            st.markdown(f"<span class='h-col-hdr'>{label}</span>", unsafe_allow_html=True)
    st.markdown("<div class='h-hdr'></div>", unsafe_allow_html=True)

    for r in rows:
        bar_pct = min(max(r["weight"], 0.0), 100.0)
        ret_col = _ret_color(r["ret_pct"])
        left, ccy, weight, shares, value, ret = st.columns([5, 1, 1, 1, 2, 1])

        with left:
            if r.get("is_bond_group"):
                if st.button("Polish retail bonds", key="hbtn_BONDS", width="stretch"):
                    _show_retail_bond_details(bond_detail_rows, base_ccy)
            else:
                btn_label = f"{r['ticker']}  |  {r['name']}" if r["name"] != r["ticker"] else r["ticker"]
                if st.button(
                    btn_label,
                    key=f"hbtn_{r['ticker']}",
                    help=f"Trade history for {r['ticker']}",
                    width='stretch',
                ):
                    show_trade_dialog(r["ticker"], r["name"], r["ccy"])
            st.markdown(
                f"<div style=\"margin-top:-0.6rem;margin-bottom:0.2rem;border-radius:4px;overflow:hidden;height:4px;background:{T['holdings_bar_bg']};\">"
                f"<div style=\"width:{bar_pct:.1f}%;height:100%;background:{ACCENT};border-radius:4px;\"></div></div>",
                unsafe_allow_html=True,
            )
        with ccy:
            st.markdown(f"<div class='h-cell'>{r['ccy']}</div>", unsafe_allow_html=True)
        with weight:
            st.markdown(f"<div class='h-cell'>{r['weight']:.1f}%</div>", unsafe_allow_html=True)
        with shares:
            st.markdown(f"<div class='h-cell'>{r['shares']:.4f}</div>", unsafe_allow_html=True)
        with value:
            st.markdown(f"<div class='h-cell'>{_fmt_val(r['value'])}</div>", unsafe_allow_html=True)
        with ret:
            st.markdown(f"<div class='h-cell' style='color:{ret_col};font-weight:600'>{_fmt_ret(r['ret_pct'])}</div>", unsafe_allow_html=True)
        st.markdown("<div class='holdings-row'></div>", unsafe_allow_html=True)

    bond_holdings = storage.load_bond_holdings()
    if bond_holdings and bond_summary["shares"]:
        st.subheader("Retail bond details")
        inflation = load_inflation()
        with st.expander(f"All retail bonds - {len(bond_holdings)} maturity date(s)"):
            detail_rows = []
            for holding in sorted(bond_holdings, key=lambda h: h.get("maturity_date", "")):
                try:
                    value_pln = bond_value_from_holding(holding, today, inflation)
                except (KeyError, TypeError, ValueError):
                    continue
                detail_rows.append({
                    "Ticker": holding["ticker"],
                    "Maturity": holding["maturity_date"],
                    "Units": float(holding["units"]),
                    "Current value (PLN)": value_pln,
                })
            if detail_rows:
                st.dataframe(detail_rows, hide_index=True, width="stretch")
