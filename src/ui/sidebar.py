from __future__ import annotations

import html
import json
import time
from datetime import date, datetime, timedelta

import streamlit as st

import config as cfg_module
from bossa_import import import_bossa
from bonds import is_retail_bond
from currencies import SUPPORTED_CURRENCIES
from ledger_core import remap_tickers
from manual_import import import_manual
from ui.styles import render_project_banner
from xtb_import import import_xtb

BROKERS = ["XTB", "BOSSA", "Custom"]
BROKER_CURRENCIES = {"XTB": sorted(SUPPORTED_CURRENCIES), "BOSSA": ["EUR", "PLN", "Many"]}


def _run_refresh(storage, today, detect_currency, base_ccy):
    """Re-import all broker files and invalidate cached data."""
    _imports_dir = storage.imports_dir()
    all_files = []
    for b in BROKERS:
        bdir = _imports_dir / b.lower()
        if not bdir.exists():
            continue
        for fpath in sorted(bdir.glob("*.xlsx")):
            all_files.append(("xtb", fpath))
        for fpath in sorted(bdir.glob("*.csv")):
            all_files.append(("bossa", fpath))
        for fpath in sorted(bdir.glob("*.json")):
            all_files.append(("custom", fpath))

    total_imported = 0
    if all_files:
        bar = st.progress(0, text="Importing…")
        for idx, (kind, fpath) in enumerate(all_files):
            ccy = detect_currency(fpath.name)
            bar.progress(idx / len(all_files), text=f"Importing {fpath.name}…")
            if kind == "bossa":
                result = import_bossa(str(fpath), ccy)
            elif kind == "custom":
                result = import_manual(str(fpath))
            else:
                result = import_xtb(str(fpath), ccy)
            if result["success"]:
                total_imported += result["imported"]
        bar.progress(1.0, text="Done")
        bar.empty()

    storage.invalidate_portfolio_from((today - timedelta(days=1)).isoformat())
    st.session_state.pop(f"snapshots_{base_ccy}_D", None)
    for k in list(st.session_state.keys()):
        if k.startswith("benchmarks_"):
            st.session_state.pop(k)
    st.session_state["force_refresh"] = True

    return len(all_files), total_imported


def _should_auto_refresh(storage, project_name, today):
    """Return True if the last refresh was more than 24 h ago (or never ran)."""
    last = storage.get_last_refresh(project_name)
    if not last:
        return True
    try:
        return (today - date.fromisoformat(last)).days >= 1
    except (ValueError, TypeError):
        return True
def _add_transaction_to_ledger(tx_date, entries, storage, data_start_date, base_ccy):
    """Write a custom JSON file and import it as a new transaction."""
    custom_dir = storage.imports_dir() / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    tx_doc = [{"date": tx_date.isoformat(), "entries": entries}]
    tx_path = custom_dir / f"{tx_date.isoformat()}_{datetime.now().strftime('%H%M%S')}.json"
    tx_path.write_text(json.dumps(tx_doc, indent=2), encoding="utf-8")
    result = import_manual(str(tx_path))
    if result["success"]:
        st.success(f"Added for {tx_date}.")
    else:
        st.error(result["error"])
    st.session_state["force_refresh"] = True
    for k in list(st.session_state.keys()):
        if k.startswith("snapshots_") or k.startswith("benchmarks_"):
            st.session_state.pop(k)
    storage.invalidate_portfolio_from(data_start_date.isoformat())
    st.rerun()


def render_sidebar(cfg, storage, T, today, data_start_date, detect_currency):
    with st.sidebar:
        project_name = html.escape(storage.get_current_project())
        st.markdown(render_project_banner(project_name, T), unsafe_allow_html=True)

        projects = storage.list_projects()
        current = storage.get_current_project()

        if current and current in projects:
            idx = projects.index(current)
        else:
            idx = 0

        selected = st.selectbox(
            "Project",
            options=projects + ["➕ New project"],
            index=idx,
            key="project_select",
        )

        if selected == "➕ New project":
            @st.dialog("Create new project")
            def _create_dialog():
                name = st.text_input("Project name", placeholder="e.g. Retirement, Savings")
                if st.button("Create", width='stretch'):
                    if name and name.strip():
                        try:
                            storage.create_project(name.strip())
                            cfg_module.invalidate_config_cache()
                            for k in list(st.session_state.keys()):
                                if (k.startswith("snapshots_") or k.startswith("benchmarks_")
                                        or k == "broker_select" or "_upload" in k):
                                    st.session_state.pop(k)
                            st.session_state["project_select"] = name.strip()
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                    else:
                        st.error("Enter a name.")
            _create_dialog()
        elif selected != current:
            storage.set_current_project(selected)
            cfg_module.invalidate_config_cache()
            for k in list(st.session_state.keys()):
                if (k.startswith("snapshots_") or k.startswith("benchmarks_")
                        or k == "broker_select" or "_upload" in k):
                    st.session_state.pop(k)
            st.rerun()

        st.markdown(
            f"<div style='color:{T['text']};font-size:0.85rem;font-weight:600;"
            f"margin-bottom:0.25rem'>Currency</div>",
            unsafe_allow_html=True,
        )
        ccy_options = ["PLN", "EUR", "USD"]
        ccy_default = ccy_options.index(cfg.get("default_currency", "PLN"))
        if "base_ccy_idx" not in st.session_state:
            st.session_state["base_ccy_idx"] = ccy_default
        ccy_cols = st.columns(3)
        base_ccy = None
        for i, ccy in enumerate(ccy_options):
            with ccy_cols[i]:
                is_active = st.session_state["base_ccy_idx"] == i
                if st.button(
                    ccy,
                    key=f"ccy_btn_{ccy}",
                    width='stretch',
                    type="primary" if is_active else "secondary",
                ):
                    st.session_state["base_ccy_idx"] = i
                    base_ccy = ccy
                    st.rerun()
        if base_ccy is None:
            base_ccy = ccy_options[st.session_state["base_ccy_idx"]]

        if _should_auto_refresh(storage, storage.get_current_project(), today):
            file_count, imported = _run_refresh(storage, today, detect_currency, base_ccy)
            storage.set_last_refresh(today.isoformat())
            st.rerun()

        _range_opts = ["All time", "This year", "Previous year", "Last 3 months",
                       "Last 12 months", "Last 2 years", "Custom"]
        if "_range" not in st.session_state:
            st.session_state["_range"] = "All time"

        st.selectbox(
            "Range",
            _range_opts,
            key="range_widget",
            index=_range_opts.index(st.session_state["_range"]),
        )
        st.session_state["_range"] = st.session_state["range_widget"]

        range_option = st.session_state["_range"]
        if range_option == "All time":
            chart_start, chart_end = data_start_date, today
        elif range_option == "This year":
            chart_start, chart_end = date(today.year, 1, 1), today
        elif range_option == "Previous year":
            chart_start, chart_end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
        elif range_option == "Last 12 months":
            chart_start, chart_end = today - timedelta(days=365), today
        elif range_option == "Last 2 years":
            chart_start, chart_end = today - timedelta(days=730), today
        elif range_option == "Last 3 months":
            chart_start, chart_end = today - timedelta(days=90), today
        else:
            ca, cb = st.columns(2)
            with ca:
                chart_start = st.date_input(
                    "From",
                    value=data_start_date,
                    min_value=data_start_date,
                    max_value=today,
                    key="range_from",
                )
            with cb:
                chart_end = st.date_input(
                    "To",
                    value=today,
                    min_value=data_start_date,
                    max_value=today,
                    key="range_to",
                )

        st.session_state["chart_start"] = chart_start
        st.session_state["chart_end"] = chart_end

        with st.expander("⚙️ Settings"):
            st.subheader("Appearance")
            current_theme = st.session_state.get("theme", "dark")
            light_on = st.toggle(
                "Light mode",
                value=(current_theme == "light"),
                key="theme_toggle",
            )
            new_theme = "light" if light_on else "dark"
            if new_theme != current_theme:
                st.session_state["theme"] = new_theme
                cfg_module.save_theme(new_theme)
                # Tell app.py to cover the next rerun with the theme veil so
                # the switch repaints in one visible step.
                st.session_state["theme_fade"] = time.time()
                st.rerun()

            st.subheader("Ticker rules")
            rules_text = st.text_area(
                "Rules",
                value="\n".join(cfg.get("ticker_rules", [])),
                height=200,
                key="ticker_rules_text",
                label_visibility="collapsed",
                placeholder="AMZN.DE=AMZ.DE\n*.PL=*.WA\n.US=",
            )
            if st.button("Save ticker rules"):
                new_rules = [line.strip() for line in rules_text.strip().splitlines() if line.strip()]
                cfg["ticker_rules"] = new_rules
                cfg_module.save(cfg)
                changed = remap_tickers()
                if changed:
                    st.success(f"Rules saved! Remapped {changed} entr{'y' if changed == 1 else 'ies'} in ledger.")
                else:
                    st.success("Rules saved!")
                st.rerun()

            st.subheader("Project")
            rename_val = st.text_input("Rename project to", value=storage.get_current_project() or "", key="rename_proj_input")
            if st.button("Rename", key="rename_proj_btn"):
                if rename_val and rename_val.strip() and rename_val.strip() != storage.get_current_project():
                    try:
                        storage.rename_project(storage.get_current_project(), rename_val.strip())
                        st.session_state.clear()
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

        with st.expander("🇵🇱 Polish retail bonds"):
            st.caption("Enter the issue ticker, maturity date and number of bonds. Rates are fetched automatically.")
            existing_bond_holdings = storage.load_bond_holdings()
            if existing_bond_holdings:
                saved_totals = ", ".join(
                    f"{holding['ticker']}: {float(holding['units']):g} units"
                    for holding in existing_bond_holdings
                )
                st.caption(f"Saved totals: {saved_totals}. New units are added to the existing series.")
            with st.form("bond_holding_form", clear_on_submit=True):
                bond_ticker = st.text_input(
                    "Ticker",
                    placeholder="EDO1233 or TOS0827",
                ).strip().upper().replace("/", "")
                bond_maturity = st.date_input(
                    "Maturity date",
                    value=today,
                    min_value=date(2000, 1, 1),
                    max_value=date(2100, 12, 31),
                )
                bond_units = st.number_input(
                    "Units to add",
                    min_value=0.0001,
                    value=1.0,
                    step=1.0,
                    format="%.4f",
                )
                bond_submitted = st.form_submit_button("Save bond holding", width="stretch")

            if bond_submitted:
                if not is_retail_bond(bond_ticker):
                    st.error("Supported tickers: EDO1233 or TOS0827.")
                else:
                    try:
                        total_units = storage.add_bond_units(
                            bond_ticker, bond_maturity.isoformat(), float(bond_units)
                        )
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        storage.invalidate_portfolio_from(data_start_date.isoformat())
                        for k in list(st.session_state.keys()):
                            if k.startswith("snapshots_") or k.startswith("benchmarks_"):
                                st.session_state.pop(k)
                        st.success(f"Added {bond_units:g}; {bond_ticker} total is now {total_units:g} units.")
                        st.rerun()

        with st.expander("➕ Add transaction"):
            # Dynamic row count managed via session state
            if "tx_rows" not in st.session_state:
                st.session_state["tx_rows"] = 2

            def _add_row():
                st.session_state["tx_rows"] += 1

            def _remove_row():
                if st.session_state["tx_rows"] > 1:
                    st.session_state["tx_rows"] -= 1

            with st.form("add_tx", clear_on_submit=True):
                tx_date = st.date_input("Date", value=today, max_value=today)
                st.caption("Negative amount = sell / cash out.")

                rows = []
                for idx in range(1, st.session_state["tx_rows"] + 1):
                    c1, c2 = st.columns([2, 1])
                    with c1:
                        t = st.text_input(
                            f"Ticker {idx}",
                            placeholder="AAPL / QDVE.DE / USD / PLN",
                            key=f"t{idx}",
                        ).strip().upper()
                    with c2:
                        a = st.number_input(
                            f"Amount {idx}",
                            value=0.0,
                            format="%.4f",
                            step=0.001,
                            key=f"a{idx}",
                        )
                    rows.append((t, a))

                col1, col2 = st.columns(2)
                with col1:
                    st.form_submit_button("➕ Add row", on_click=_add_row)
                with col2:
                    if st.session_state["tx_rows"] > 1:
                        st.form_submit_button("➖ Remove row", on_click=_remove_row)

                is_account_op = st.checkbox(
                    "Account operation (deposit/withdrawal)",
                    key="ao_new",
                    help="Marks this transaction as invested capital",
                )

                submitted = st.form_submit_button("Add transaction", width="stretch")

                if submitted:
                    entries = [{
                        "ticker": t,
                        "amount": a,
                        **({"account_operation": True} if is_account_op else {})
                    } for t, a in rows if t and abs(a) > 1e-9]

                    if not entries:
                        st.error("Enter at least one ticker and amount.")
                    else:
                        # Real-time balance check: simulate post-transaction positions
                        current = storage.load_balance()
                        simulated = {k: v["amount"] for k, v in current.items()}
                        for e in entries:
                            ticker = e["ticker"]
                            if ticker not in storage.SUPPORTED_CURRENCIES:
                                simulated[ticker] = simulated.get(ticker, 0.0) + e["amount"]
                        negatives = [
                            (ticker, amt) for ticker, amt in simulated.items()
                            if amt < -1e-9 and ticker not in storage.SUPPORTED_CURRENCIES
                        ]
                        if negatives:
                            details = ", ".join(f"{t}: {a:+.4f}" for t, a in negatives)
                            st.warning(
                                f"⚠️ This transaction would make positions negative: {details}\n\n"
                                "Check the amounts or add a buy first."
                            )
                            if st.checkbox("I understand — proceed anyway", key="proceed_anyway"):
                                _add_transaction_to_ledger(tx_date, entries, storage, data_start_date, base_ccy)
                        else:
                            _add_transaction_to_ledger(tx_date, entries, storage, data_start_date, base_ccy)

        with st.expander("📥 Import statement"):
            _proj = storage.get_current_project()
            _imports = storage.imports_dir()
            _imports.mkdir(parents=True, exist_ok=True)

            broker = st.selectbox("Broker", BROKERS, key="broker_select")
            broker_dir = _imports / broker.lower()
            broker_dir.mkdir(parents=True, exist_ok=True)

            if broker == "XTB":
                st.info("ℹ️ Name your file starting with the currency code, e.g. `EUR_history.xlsx` — the currency is auto-detected from the prefix.")

            file_types = ["csv"] if broker == "BOSSA" else ["json"] if broker == "Custom" else ["xlsx"]
            uploaded_files = st.file_uploader(
                f"Upload {broker} files",
                type=file_types,
                accept_multiple_files=True,
                key=f"{_proj}_{broker}_upload",
                label_visibility="collapsed",
            )

            if uploaded_files:
                for uf in uploaded_files:
                    dest = broker_dir / uf.name
                    dest.write_bytes(uf.getvalue())
                    detected = detect_currency(uf.name)
                    if broker == "BOSSA":
                        ccy = "Many"
                        with st.spinner(f"Importing {uf.name}…"):
                            result = import_bossa(str(dest), ccy)
                    elif broker == "Custom":
                        with st.spinner(f"Importing {uf.name}…"):
                            result = import_manual(str(dest))
                    else:
                        ccy_options = BROKER_CURRENCIES.get(broker, sorted(SUPPORTED_CURRENCIES))
                        if detected not in ccy_options:
                            detected = ccy_options[0]
                        with st.spinner(f"Importing {uf.name}…"):
                            result = import_xtb(str(dest), detected)
                    if result["success"]:
                        n = result["imported"]
                        s = result["skipped"]
                        msg = f"**{uf.name}** — {n} imported"
                        if s:
                            msg += f", {s} skipped (duplicates)"
                        st.success(msg)
                    else:
                        st.error(f"**{uf.name}** — {result['error']}")
                st.session_state["force_refresh"] = True
                st.session_state.pop(f"{_proj}_{broker}_upload", None)
                for k in list(st.session_state.keys()):
                    if k.startswith("snapshots_") or k.startswith("benchmarks_"):
                        st.session_state.pop(k)
                storage.invalidate_portfolio_from(data_start_date.isoformat())
                st.rerun()

            broker_files = sorted(broker_dir.glob("*.xlsx")) + sorted(broker_dir.glob("*.csv")) + sorted(broker_dir.glob("*.json"))
            if broker_files:
                for fpath in broker_files:
                    st.caption(f"📄 {fpath.name}")
            else:
                st.caption("No files uploaded yet.")

        if st.button("📈  Refresh data", width="stretch"):
            file_count, total_imported = _run_refresh(storage, today, detect_currency, base_ccy)
            if file_count:
                st.success(f"Refreshed from {file_count} files — {total_imported} transactions imported.")
            else:
                st.info("No import files found.")

            storage.set_last_refresh(today.isoformat())
            st.rerun()

    return base_ccy
