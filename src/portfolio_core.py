"""
portfolio.py — build the portfolio value time-series

portfolio.jsonl schema (one object per line, chronological):
  {
    "date": "YYYY-MM-DD",
    "assets": [
      {"ticker": "AAPL", "amount": 10.0, "price": 214.5, "currency": "USD",
       "value_native": 2145.0, "value_base": 8750.0},
      ...
    ],
    "total_value": 14300.0,       <- in base currency
    "invested": 12000.0,      <- cumulative net deposits in base currency
    "base_currency": "PLN"
  }

invested rule:
  Only entries marked ``account_operation`` (deposits, withdrawals) count
  toward invested capital.  Everything else — stock buys/sells, dividends,
  interest, taxes, commissions, FX swaps — never counts, even when it is a
  pure-cash transaction. Dividends/interest simply credit cash inside the
  account (raising total_value without raising invested), so they show up
  as performance in P&L, TWR and IRR.
  - PLN +5000  (account_operation)       -> deposit, counts
  - PLN -2000  (account_operation)       -> withdrawal, counts
  - AAPL +10, USD -1700                  -> stock buy, neither counts
  - AAPL -10, USD +2100                  -> stock sell, neither counts
  - PLN +10000 (account_operation), AAPL +10, USD -1700
      -> deposit counts, stock buy does not
  - PLN +50   (dividend / interest)      -> cash gain, does NOT count

Key optimisations:
  1. Single forward pass: O(days + tx) not O(days x tx).
  2. _PriceCache loads each ticker-year slab once per build, never twice.
  3. Binary orjson I/O in storage layer.
  4. Resume: only days after last cached snapshot are recomputed.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Callable

import storage

log = logging.getLogger(__name__)

from storage import (
    load_portfolio,
    save_portfolio,
    SUPPORTED_CURRENCIES,
    SUFFIX_CURRENCY,
)
from ticker_data import get_price, get_fx_rate, get_ticker_currency, FX_YAHOO
from ledger_core import get_all_transactions
from bonds import (
    bond_value,
    bond_value_from_holding,
    bond_purchase_date,
    is_retail_bond,
    load_bond_definition,
    load_inflation,
    refresh_monthly_cpi_from_gus,
)

FX_TICKERS = set(FX_YAHOO.keys())


# -- Price cache ---------------------------------------------------------------

class _PriceCache:
    """
    Lazy per-ticker, per-year price slab.
    Loads each (ticker, year) pair exactly once per build_portfolio() call.
    """
    def __init__(self):
        self._data: dict[str, dict[int, dict[str, float]]] = {}

    def get(self, ticker: str, day: str, year: int) -> float | None:
        if ticker in SUPPORTED_CURRENCIES:
            return 1.0
        return get_price(ticker, day, self._data, year)

    def get_fx(self, from_ccy: str, to_ccy: str, day: str, year: int) -> float:
        return get_fx_rate(from_ccy, to_ccy, day, self._data, year)


# -- Ticker currency detection -------------------------------------------------

def _ticker_currency(ticker: str) -> str:
    t = ticker.upper()
    if t in SUPPORTED_CURRENCIES:
        return t
    ext = t[t.rfind("."):] if "." in t else ""
    ccy = SUFFIX_CURRENCY.get(ext)
    if ccy:
        return ccy
    if t in FX_TICKERS:
        return "PLN"
    if ext:
        log.warning("unknown exchange suffix '%s' for ticker %s, assuming USD", ext, t)
    return "USD"


def _apply_bond_transaction(
    lots: dict[str, list[tuple[date, float]]],
    ticker: str,
    amount: float,
    transaction_date: date,
) -> None:
    if amount > 0:
        lots.setdefault(ticker, []).append((transaction_date, amount))
        return
    if amount >= 0:
        return

    remaining_to_sell = -amount
    remaining_lots = []
    for purchase_date, units in lots.get(ticker, []):
        sold_units = min(units, remaining_to_sell)
        units -= sold_units
        remaining_to_sell -= sold_units
        if units > 1e-9:
            remaining_lots.append((purchase_date, units))

    if remaining_lots:
        lots[ticker] = remaining_lots
    else:
        lots.pop(ticker, None)


# -- Main build function -------------------------------------------------------

def build_portfolio(
    start_date: date,
    end_date: date,
    base_currency: str,
    precision: str,                               # "D" or "W-FRI"
    progress_cb: Callable[[str, float], None] | None = None,
    use_cache: bool = True,
) -> list[dict]:
    """
    Build or resume the portfolio value time-series.

    Single forward pass: O(days + transactions).
    Returns list of snapshot dicts, chronological. Also persists to portfolio.jsonl.
    """
    base_currency = base_currency.upper()
    cache = _PriceCache()
    bond_holdings = storage.load_bond_holdings()
    bond_schedule = []
    for holding in bond_holdings:
        try:
            definition = load_bond_definition(holding["ticker"])
            purchase_date = bond_purchase_date(holding, definition)
            bond_schedule.append((purchase_date, holding))
        except (KeyError, TypeError, ValueError):
            continue

    # -- Resume from cache -----------------------------------------------------
    existing: list[dict] = []
    if use_cache:
        existing = [s for s in load_portfolio()
                    if s.get("base_currency") == base_currency]
    if existing and date.fromisoformat(existing[0]["date"]) > start_date:
        existing = []

    if existing:
        last_cached        = existing[-1]["date"]
        resume_from        = date.fromisoformat(last_cached) + timedelta(days=1)
        last_snap          = existing[-1]
        balance: dict[str, float] = {
            a["ticker"]: a["amount"] for a in last_snap["assets"]
        }
        cumulative_contrib = last_snap["invested"]
    else:
        resume_from        = start_date
        balance            = {}
        cumulative_contrib = sum(
            float(holding["units"]) * 100.0
            for purchase_date, holding in bond_schedule
            if purchase_date < resume_from
        )

    # -- Load transactions once ------------------------------------------------
    today_str = date.today().isoformat()
    all_tx     = get_all_transactions()
    bond_purchase_years = []
    bond_definitions = {}
    bond_lots: dict[str, list[tuple[date, float]]] = {}
    for record in all_tx:
        transaction_date = date.fromisoformat(record["date"])
        for entry in record["entries"]:
            ticker = entry["ticker"].upper()
            if not is_retail_bond(ticker):
                continue
            amount = float(entry["amount"])
            if ticker not in bond_definitions:
                bond_definitions[ticker] = load_bond_definition(ticker)
            if amount > 0:
                bond_purchase_years.append(transaction_date.year)
            if existing and transaction_date < resume_from:
                _apply_bond_transaction(bond_lots, ticker, amount, transaction_date)
    if bond_purchase_years or bond_schedule:
        first_bond_year = min(
            [purchase.year for purchase, _ in bond_schedule]
            + bond_purchase_years
        )
        inflation = refresh_monthly_cpi_from_gus(first_bond_year, end_date.year)
    else:
        inflation = load_inflation()
    resume_str = resume_from.isoformat()
    pending_tx = [r for r in all_tx if r["date"] >= resume_str and r["date"] <= today_str]
    tx_idx     = 0
    n_tx       = len(pending_tx)

    # -- Day iteration (forward pass) ------------------------------------------
    all_days      = list(_day_range(resume_from, end_date, precision))
    total_days    = max(len(all_days), 1)
    new_snapshots: list[dict] = []

    for i, day in enumerate(all_days):
        day_str = day.isoformat()
        year    = day.year

        if progress_cb and i % 10 == 0:
            progress_cb(day_str, i / total_days)

        # Apply pending transactions up to this day
        while tx_idx < n_tx and pending_tx[tx_idx]["date"] <= day_str:
            rec     = pending_tx[tx_idx]
            tx_year = int(rec["date"][:4])

            # invested rule: ONLY account_operation entries (deposits /
            # withdrawals) count toward invested capital. Unmarked pure-cash
            # entries — dividends, interest, taxes, commissions, FX swaps —
            # credit cash inside the account without raising invested, so they
            # read as performance (P&L / TWR / IRR), not as new capital.
            entries_list = rec["entries"]

            for e in entries_list:
                t   = e["ticker"].upper()
                amt = float(e["amount"])
                balance[t] = balance.get(t, 0.0) + amt
                if is_retail_bond(t):
                    _apply_bond_transaction(
                        bond_lots, t, amt, date.fromisoformat(rec["date"])
                    )

                if e.get("account_operation", False):
                    fx = cache.get_fx(t, base_currency, rec["date"], tx_year)
                    cumulative_contrib += amt * fx

            tx_idx += 1

        if not existing:
            cumulative_contrib += sum(
                float(holding["units"]) * 100.0
                for purchase_date, holding in bond_schedule
                if purchase_date == day
            )

        # Remove dust positions
        balance = {k: v for k, v in balance.items() if abs(v) > 1e-9}

        if not balance and not bond_holdings:
            new_snapshots.append({
                "date": day_str, "assets": [],
                "total_value": 0.0, "invested": 0.0,
                "base_currency": base_currency,
            })
            continue

        # Value each position
        assets      = []
        total_value = 0.0

        for ticker, amount in balance.items():
            t = ticker.upper()
            if t in FX_TICKERS:
                continue                       # internal FX helper, not displayed

            if t in SUPPORTED_CURRENCIES:
                rate       = cache.get_fx(t, base_currency, day_str, year)
                value_base = round(amount * rate, 2)
                assets.append({
                    "ticker":       t,
                    "amount":       round(amount, 8),
                    "price":        1.0,
                    "currency":     t,
                    "value_native": round(amount, 2),
                    "value_base":   value_base,
                })
                total_value += value_base
            elif is_retail_bond(t) and bond_definitions.get(t):
                value_native = sum(
                    bond_value(bond_definitions[t], units, purchase_date, day, inflation)
                    for purchase_date, units in bond_lots.get(t, [])
                )
                rate = cache.get_fx("PLN", base_currency, day_str, year)
                value_base = round(value_native * rate, 2)
                assets.append({
                    "ticker": t,
                    "amount": round(amount, 8),
                    "price": round(value_native / amount, 6) if amount else 0.0,
                    "currency": "PLN",
                    "value_native": value_native,
                    "value_base": value_base,
                })
                total_value += value_base
            else:
                price = cache.get(t, day_str, year)
                if price is None:
                    continue
                # Currency must match how Yahoo quotes the price series for
                # this exact symbol — suffix-based guessing misclassifies
                # USD-quoted LSE lines (.L is not always GBP) and dead
                # symbols. get_ticker_currency falls back to the suffix map.
                ticker_ccy   = get_ticker_currency(t)
                value_native = round(amount * price, 2)
                rate         = cache.get_fx(ticker_ccy, base_currency, day_str, year)
                value_base   = round(value_native * rate, 2)
                assets.append({
                    "ticker":       t,
                    "amount":       round(amount, 8),
                    "price":        round(price, 6),
                    "currency":     ticker_ccy,
                    "value_native": value_native,
                    "value_base":   value_base,
                })
                total_value += value_base

        # Holdings entered from a bank statement do not need a synthetic
        # transaction: their quantity and maturity date are enough to value them.
        displayed_tickers = {asset["ticker"] for asset in assets}
        bond_asset_totals: dict[str, dict[str, float]] = {}
        for holding in bond_holdings:
            ticker = str(holding.get("ticker", "")).upper()
            if ticker in displayed_tickers or not is_retail_bond(ticker):
                continue
            try:
                definition = load_bond_definition(ticker)
                if day < bond_purchase_date(holding, definition):
                    continue
                value_native = bond_value_from_holding(holding, day, inflation)
                units = float(holding["units"])
            except (KeyError, TypeError, ValueError):
                continue
            total = bond_asset_totals.setdefault(ticker, {"units": 0.0, "value_native": 0.0})
            total["units"] += units
            total["value_native"] += value_native

        rate = cache.get_fx("PLN", base_currency, day_str, year)
        for ticker, total in bond_asset_totals.items():
            units = total["units"]
            value_native = round(total["value_native"], 2)
            value_base = round(value_native * rate, 2)
            assets.append({
                "ticker": ticker,
                "amount": round(units, 8),
                "price": round(value_native / units, 6) if units else 0.0,
                "currency": "PLN",
                "value_native": value_native,
                "value_base": value_base,
            })
            total_value += value_base

        new_snapshots.append({
            "date":          day_str,
            "assets":        assets,
            "total_value":   round(total_value, 2),
            "invested":  round(cumulative_contrib, 2),
            "base_currency": base_currency,
        })

    if progress_cb:
        progress_cb(end_date.isoformat(), 1.0)

    # -- Merge, deduplicate, persist -------------------------------------------
    merged = _merge_snapshots(existing, new_snapshots)
    save_portfolio(merged)
    return merged


def _merge_snapshots(existing: list[dict], new: list[dict]) -> list[dict]:
    if not existing:
        return new
    if not new:
        return existing
    seen: dict[str, dict] = {s["date"]: s for s in existing}
    for s in new:
        seen[s["date"]] = s
    return sorted(seen.values(), key=lambda x: x["date"])


# -- Day range generator -------------------------------------------------------

def _day_range(start: date, end: date, precision: str):
    if precision == "D":
        d = start
        while d <= end:
            yield d
            d += timedelta(days=1)
    else:
        d = start
        d += timedelta(days=(4 - d.weekday()) % 7)   # jump to first Friday
        while d <= end:
            yield d
            d += timedelta(weeks=1)


# -- Series extraction ---------------------------------------------------------

def snapshots_to_series(snapshots: list[dict]) -> tuple[list[str], list[float], list[float]]:
    dates: list[str] = []
    values: list[float] = []
    contrs: list[float] = []
    for s in snapshots:
        dates.append(s["date"])
        values.append(s["total_value"])
        contrs.append(s["invested"])
    return dates, values, contrs
