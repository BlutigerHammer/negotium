"""
ledger_core.py — transaction ledger management

transactions.jsonl schema (one object per line, chronological):
  {"date": "YYYY-MM-DD", "entries": [
      {"ticker": STR, "amount": FLOAT, "account_operation": BOOL}, ...
  ]}

  account_operation (optional, per entry): marks deposits/withdrawals
  that count toward invested capital.

balance.json schema:
  {"AAPL": 10.0, "PLN": 5000.0, ...}

Rules:
- File must stay chronological (ascending date).
- Inserting a transaction for an existing date: merge entries into that line.
- Inserting for a date earlier than the last line: find the correct position,
  insert, rewrite file, then invalidate portfolio cache from that date.
- Inserting for a new date after all existing: append.
"""
from __future__ import annotations

import functools
import logging
import threading
from datetime import date

import storage
import config as cfg_module
from ticker_translate import translate_ticker
from ticker_data import get_price

log = logging.getLogger(__name__)

# Serialise ledger mutations. Streamlit runs each session in a thread and
# every mutator does read-modify-write on the same JSONL file — without the
# lock two concurrent reruns can interleave and lose transactions.
_ledger_lock = threading.RLock()


def _locked(fn):
    """Run a ledger mutator under the process-wide ledger lock."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _ledger_lock:
            return fn(*args, **kwargs)
    return wrapper


def _apply_entries(balance: dict[str, dict], entries: list[dict]) -> None:
    for e in entries:
        ticker = e["ticker"].upper()
        amount = float(e["amount"])
        if ticker not in balance:
            balance[ticker] = {"amount": 0.0, "avg_price": 0.0}
        balance[ticker]["amount"] = balance[ticker]["amount"] + amount
        if abs(balance[ticker]["amount"]) < 1e-9:
            balance[ticker]["amount"] = 0.0
            balance[ticker]["avg_price"] = 0.0


@_locked
def add_transaction(
    tx_date: date | str,
    entries: list[dict],
    account_operation: bool = False,
) -> None:
    """
    Add a transaction for the given date.

    entries: [{"ticker": "AAPL", "amount": 10.0}, {"ticker": "USD", "amount": -1710.0}]
    account_operation: if True, every entry gets account_operation=True
                       (marks deposits/withdrawals that count as invested).
    """
    if isinstance(tx_date, date):
        date_str = tx_date.isoformat()
    else:
        date_str = tx_date

    # Normalise
    rules = cfg_module.load().get("ticker_rules", [])
    entries = [
        {"ticker": translate_ticker(e["ticker"], rules), "amount": round(float(e["amount"]), 8),
         **({"account_operation": True} if account_operation or e.get("account_operation") else {})}
        for e in entries
    ]

    records = storage.read_jsonl(storage.transactions_path())

    if not records:
        records = [{"date": date_str, "entries": entries}]
        storage.write_jsonl(storage.transactions_path(), records)
    else:
        last_date = records[-1]["date"]

        if date_str > last_date:
            # Fast append — new date after everything
            records.append({"date": date_str, "entries": entries})
            storage.write_jsonl(storage.transactions_path(), records)
        elif date_str == last_date:
            # Merge into the last record
            records[-1]["entries"].extend(entries)
            storage.write_jsonl(storage.transactions_path(), records)
        else:
            # Past date — find insertion point, rewrite whole file
            new_records: list[dict] = []
            inserted = False
            for rec in records:
                if not inserted:
                    if rec["date"] == date_str:
                        # Merge into existing entry for this date
                        rec = dict(rec)  # shallow copy to avoid mutating original
                        rec["entries"] = rec["entries"] + entries
                        new_records.append(rec)
                        inserted = True
                        continue
                    elif rec["date"] > date_str:
                        # Insert before this record
                        new_records.append({"date": date_str, "entries": entries})
                        inserted = True
                new_records.append(rec)

            if not inserted:
                new_records.append({"date": date_str, "entries": entries})

            records = new_records
            storage.write_jsonl(storage.transactions_path(), records)

    # Balance is ALWAYS rebuilt by replaying the full ledger. An earlier
    # optimisation replayed only records on/after the touched date on top of
    # the persisted balance — but that balance already included those records,
    # so every back-dated insert and same-date merge double-counted entries
    # and silently corrupted balance.json.
    _rebuild_balance(records)
    storage.invalidate_portfolio_from(date_str)


@_locked
def set_account_operation(date_str: str, entry_idx: int, value: bool) -> None:
    """Set or clear the account_operation flag on a specific entry.

    entry_idx: index of the entry within the transaction's entries list.
    """
    records = storage.read_jsonl(storage.transactions_path())
    for rec in records:
        if rec["date"] == date_str:
            entries = rec["entries"]
            if 0 <= entry_idx < len(entries):
                if value:
                    entries[entry_idx]["account_operation"] = True
                else:
                    entries[entry_idx].pop("account_operation", None)
                storage.write_jsonl(storage.transactions_path(), records)
                storage.invalidate_portfolio_from(date_str)
            break


@_locked
def delete_transaction(date_str: str, entry_idx: int) -> None:
    """Remove a single entry from a transaction. Removes the record if empty."""
    records = storage.read_jsonl(storage.transactions_path())
    new_records: list[dict] = []
    for rec in records:
        if rec["date"] == date_str:
            if 0 <= entry_idx < len(rec["entries"]):
                rec["entries"].pop(entry_idx)
                if rec["entries"]:
                    new_records.append(rec)
            else:
                new_records.append(rec)
        else:
            new_records.append(rec)
    storage.write_jsonl(storage.transactions_path(), new_records)
    _rebuild_balance(new_records)
    storage.invalidate_portfolio_from(date_str)


@_locked
def update_transaction(
    date_str: str,
    entry_idx: int,
    ticker: str,
    amount: float,
    account_operation: bool = False,
) -> None:
    """Replace a single entry's ticker, amount, and account_operation flag."""
    rules = cfg_module.load().get("ticker_rules", [])
    records = storage.read_jsonl(storage.transactions_path())
    for rec in records:
        if rec["date"] == date_str:
            if 0 <= entry_idx < len(rec["entries"]):
                new_entry: dict = {
                    "ticker": translate_ticker(ticker.upper(), rules),
                    "amount": round(float(amount), 8),
                }
                if account_operation:
                    new_entry["account_operation"] = True
                rec["entries"][entry_idx] = new_entry
                storage.write_jsonl(storage.transactions_path(), records)
                _rebuild_balance(records)
                storage.invalidate_portfolio_from(date_str)
            break


@_locked
def remap_tickers() -> int:
    """Re-apply ticker_rules to every entry in the ledger.

    Returns the number of entries whose ticker changed. Safe to call when
    rules are unchanged — it detects no-ops and skips the write.
    """
    rules = cfg_module.load().get("ticker_rules", [])
    records = storage.read_jsonl(storage.transactions_path())
    changed = 0
    for rec in records:
        for i, e in enumerate(rec["entries"]):
            new_ticker = translate_ticker(e["ticker"], rules)
            if new_ticker != e["ticker"]:
                rec["entries"][i] = {**e, "ticker": new_ticker}
                changed += 1
    if changed:
        storage.write_jsonl(storage.transactions_path(), records)
        _rebuild_balance(records)
        if records:
            storage.invalidate_portfolio_from(records[0]["date"])
    return changed


@_locked
def _rebuild_balance(records: list[dict]) -> None:
    """Replay the ledger to recompute balance and avg_price from scratch.

    Always replays every record onto a fresh balance and persists the result.
    Records dated in the future are skipped (same rule the ledger replay in
    build_portfolio uses) so future-dated entries don't leak into holdings.
    """
    base_ccy = cfg_module.load().get("default_currency", "PLN")
    today_str = date.today().isoformat()

    balance: dict[str, dict] = {}
    price_cache: dict = {}
    for rec in records:
        if rec["date"] > today_str:
            continue
        _update_avg_prices(balance, rec, base_ccy, price_cache)
        _apply_entries(balance, rec["entries"])
    storage.save_balance(balance)


def _update_avg_prices(balance: dict[str, dict], rec: dict, base_ccy: str, price_cache: dict | None = None) -> None:
    """After applying entries, compute avg_price in base currency for stock buys.

    Uses the ticker's close price on the transaction date to determine cost.
    On sells, avg_price stays constant (standard weighted-average cost).
    """
    entries = rec["entries"]
    tx_date = rec["date"]
    yr = int(tx_date[:4])
    if price_cache is None:
        price_cache = {}

    # Accumulate cost and shares per ticker for this transaction
    ticker_cost: dict[str, float] = {}
    ticker_shares: dict[str, float] = {}

    for e in entries:
        ticker = e["ticker"].upper()
        amt = float(e["amount"])
        if ticker in storage.SUPPORTED_CURRENCIES or amt <= 0:
            continue

        close = get_price(ticker, tx_date, price_cache, yr)
        if close is None:
            continue

        ticker_cost[ticker] = ticker_cost.get(ticker, 0.0) + amt * close
        ticker_shares[ticker] = ticker_shares.get(ticker, 0.0) + amt

    # Collect all tickers touched by this transaction (buys + sells)
    all_tickers = set(ticker_shares.keys())
    for e in entries:
        t = e["ticker"].upper()
        if t not in storage.SUPPORTED_CURRENCIES:
            all_tickers.add(t)

    # Compute new avg_price
    # balance still has pre-tx state since _apply_entries hasn't run yet
    for ticker in all_tickers:
        pre = balance.get(ticker, {}).get("amount", 0.0)
        old_avg = balance.get(ticker, {}).get("avg_price", 0.0)

        net_change = sum(float(e["amount"]) for e in entries
                         if e["ticker"].upper() == ticker)
        new_amount = pre + net_change
        if new_amount > 0:
            old_cost = pre * old_avg
            if ticker in ticker_cost:
                # Buy: blend old cost with new cost
                new_cost = old_cost + ticker_cost[ticker]
            else:
                # Sell only: reduce cost pool proportionally
                new_cost = old_avg * new_amount
            new_avg = new_cost / new_amount
        else:
            new_avg = 0.0

        if ticker not in balance:
            balance[ticker] = {"amount": 0.0, "avg_price": new_avg}
        else:
            balance[ticker]["avg_price"] = new_avg


@_locked
def rebuild_balance() -> None:
    """Rebuild balance.json from scratch by replaying the entire ledger."""
    records = get_all_transactions()
    _rebuild_balance(records)


def compute_twr(
    snapshots: list[dict],
    base_currency: str | None = None,
    fx_cache: dict | None = None,
    end: str | None = None,
) -> float | None:
    """Compute cumulative Time-Weighted Return (TWR) from daily snapshots.

    TWR chains the daily portfolio growth factors between external cash
    flows (deposits/withdrawals), so it measures the performance of the
    investments themselves, independent of deposit timing. It is the natural
    companion to :func:`compute_irr` (money-weighted return):

        factor_d = (V_d - CF_d) / V_prev        (CF > 0 for deposits)
        TWR      = Π factor_d - 1

    where ``V_prev`` is the previous day's close and ``V_d`` today's close
    (both already including that day's flow sitting in cash). Days where the
    portfolio held no value at the previous close (before the first deposit
    or right after a full withdrawal) are skipped — no market return is
    measurable there.

    ``snapshots`` must be chronological dicts with at least ``"date"`` and
    ``"total_value"`` (in ``base_currency``). Flows are the
    ``account_operation`` ledger entries converted to base currency at each
    transaction's date — the same flow rule as :func:`compute_irr`, so
    dividends, stock buys/sells and internal FX swaps never distort TWR.

    ``end`` is an ISO date string; snapshots and flows after it are ignored,
    so the metric honours a chart's date-range end.

    Returns cumulative TWR as a decimal (e.g. 0.12 for 12%), or None when no
    measurable period exists (fewer than two snapshots, or the portfolio never
    held a positive value on two consecutive days).
    """
    if not snapshots:
        return None
    from ticker_data import get_fx_rate
    from datetime import date as _date

    if base_currency is None:
        base_currency = cfg_module.load().get("default_currency", "PLN")
    base_ccy = base_currency.upper()
    if fx_cache is None:
        fx_cache = {}

    # External flows: deposits/withdrawals only, merged per date.
    # Deposits are positive (money entering the portfolio).
    flows: dict[str, float] = {}
    records = get_all_transactions()
    for rec in records:
        if end and rec["date"] > end:
            continue
        for e in rec["entries"]:
            if not e.get("account_operation", False):
                continue
            t = e["ticker"].upper()
            amt = float(e["amount"])
            fx = get_fx_rate(t, base_ccy, rec["date"], fx_cache, int(rec["date"][:4])) if t != base_ccy else 1.0
            flows[rec["date"]] = flows.get(rec["date"], 0.0) + amt * fx

    for purchase_date, amount_pln in _manual_bond_flows(records, end):
        purchase_year = int(purchase_date[:4])
        fx = get_fx_rate("PLN", base_ccy, purchase_date, fx_cache, purchase_year) \
            if base_ccy != "PLN" else 1.0
        flows[purchase_date] = flows.get(purchase_date, 0.0) + amount_pln * fx

    cum = 1.0
    chained_days = 0
    prev_value: float | None = None
    for snap in snapshots:
        day = snap["date"]
        if end and day > end:
            break
        value = float(snap.get("total_value") or 0.0)
        cf = flows.get(day, 0.0)
        if prev_value is not None and prev_value > 1e-9:
            cum *= (value - cf) / prev_value
            chained_days += 1
        prev_value = value

    if chained_days == 0:
        return None
    return cum - 1.0


def annualize_twr(twr_cum: float, days: int) -> float:
    """Annualize a cumulative TWR: ``(1 + TWR)^(365.25 / days) - 1``.

    ``days`` is the span of the window the cumulative TWR was computed over.
    Windows shorter than one day return the cumulative value unchanged
    (annualizing is meaningless there).
    """
    if days <= 0:
        return twr_cum
    return (1.0 + twr_cum) ** (365.25 / days) - 1.0


def compute_irr(current_value: float, base_currency: str | None = None, fx_cache: dict | None = None, end: str | None = None) -> float | None:
    """Compute Internal Rate of Return (money-weighted) using all cash flows up to ``end``.

    Deposits are negative (money out of pocket), withdrawals positive.
    The current portfolio value is the final positive cash flow at ``end``
    (defaults to today). When the metric is shown for a historical range,
    pass that range's end date so the timeline stays consistent.

    Returns IRR as a decimal (e.g. 0.12 for 12%), or None if not solvable.
    """
    from ticker_data import get_fx_rate
    from datetime import date as _date

    records = get_all_transactions()
    if base_currency is None:
        base_currency = cfg_module.load().get("default_currency", "PLN")
    base_ccy = base_currency.upper()
    end_date = _date.fromisoformat(end) if end else _date.today()
    if fx_cache is None:
        fx_cache = {}

    cash_flows: list[tuple[str, float]] = []
    for rec in records:
        if end and rec["date"] > end:
            continue
        for e in rec["entries"]:
            is_entry_op = e.get("account_operation", False)
            if not is_entry_op:
                continue
            t = e["ticker"].upper()
            amt = float(e["amount"])
            fx = get_fx_rate(t, base_ccy, rec["date"], fx_cache, int(rec["date"][:4])) if t != base_ccy else 1.0
            # Negate: positive account_op = deposit (money out of pocket)
            cash_flows.append((rec["date"], -amt * fx))

    for purchase_date, amount_pln in _manual_bond_flows(records, end):
        purchase_year = int(purchase_date[:4])
        fx = get_fx_rate("PLN", base_ccy, purchase_date, fx_cache, purchase_year) \
            if base_ccy != "PLN" else 1.0
        cash_flows.append((purchase_date, -amount_pln * fx))

    if not cash_flows:
        return None

    # Merge cash flows on the same date
    merged: dict[str, float] = {}
    for d, amt in cash_flows:
        merged[d] = merged.get(d, 0.0) + amt

    # Add current portfolio value as final cash flow (positive — you own it)
    cf_list = sorted(merged.items())
    cf_list.append((end_date.isoformat(), current_value))

    # Convert dates to years from first cash flow
    start = _date.fromisoformat(cf_list[0][0])
    cf_years = [(_date.fromisoformat(d) - start).days / 365.25 for d, _ in cf_list]
    cf_amounts = [a for _, a in cf_list]

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** t for a, t in zip(cf_amounts, cf_years))

    def npv_deriv(rate: float) -> float:
        return sum(-t * a / (1.0 + rate) ** (t + 1) for a, t in zip(cf_amounts, cf_years))

    # Newton-Raphson with bisection fallback
    rate = 0.1
    for _ in range(100):
        f = npv(rate)
        if abs(f) < 0.001:
            return rate
        fp = npv_deriv(rate)
        if abs(fp) < 1e-12:
            break
        new_rate = rate - f / fp
        if new_rate < -0.5:
            new_rate = -0.5
        if new_rate > 5.0:
            new_rate = 5.0
        rate = new_rate

    # Bisection fallback
    lo, hi = -0.5, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
        if abs(hi - lo) < 1e-8:
            break
    return (lo + hi) / 2.0


def _manual_bond_flows(records: list[dict], end: str | None = None) -> list[tuple[str, float]]:
    """Return manual bond contributions not already represented by ledger units."""
    from bonds import bond_purchase_date, is_retail_bond, load_bond_definition

    flows = []
    for holding in storage.load_bond_holdings():
        try:
            ticker = str(holding["ticker"]).upper().strip()
            if not is_retail_bond(ticker):
                continue
            definition = load_bond_definition(ticker)
            purchase = bond_purchase_date(holding, definition)
            purchase_date = purchase.isoformat()
            amount = float(holding["units"]) * 100.0
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if end and purchase_date > end:
            continue

        ledger_units = sum(
            float(entry["amount"])
            for record in records
            if record["date"] <= purchase_date
            for entry in record["entries"]
            if entry["ticker"].upper() == ticker
        )
        if abs(ledger_units) > 1e-9:
            continue
        flows.append((purchase_date, amount))
    return flows


def get_all_transactions() -> list[dict]:
    """Return all transactions, chronologically (cached per project ledger).

    Thread-safe: concurrent readers and writers are serialised so a
    reader never sees a half-cleared cache.
    """
    path = storage.transactions_path()
    try:
        stat = path.stat()
        cache_key = ("_tx_cache", str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        cache_key = ("_tx_cache", str(path.resolve()), 0, 0)
    with _tx_cache_lock:
        if cache_key not in get_all_transactions._cache:
            get_all_transactions._cache.clear()
            get_all_transactions._cache[cache_key] = storage.read_jsonl(path)
        return get_all_transactions._cache[cache_key]

get_all_transactions._cache: dict = {}
_tx_cache_lock = threading.Lock()


def first_transaction_date() -> date | None:
    """Return the earliest transaction date, or None if there are none.

    Used as the data-driven floor for chart ranges, the custom-range picker
    and portfolio (re)computation — replaces the old config ``start_day``.
    """
    txs = get_all_transactions()
    if not txs:
        return None
    return date.fromisoformat(min(r["date"] for r in txs))


def first_portfolio_date() -> date | None:
    """Return the earliest ledger or manually held retail-bond purchase date."""
    dates = []
    first_tx = first_transaction_date()
    if first_tx is not None:
        dates.append(first_tx)

    from bonds import bond_purchase_date, is_retail_bond, load_bond_definition
    for holding in storage.load_bond_holdings():
        try:
            ticker = str(holding.get("ticker", "")).upper().strip()
            if not is_retail_bond(ticker):
                continue
            definition = load_bond_definition(ticker)
            dates.append(bond_purchase_date(holding, definition))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue

    return min(dates) if dates else None


def get_transactions_up_to(as_of: str) -> list[dict]:
    """Return all transactions with date <= as_of."""
    return [r for r in get_all_transactions() if r["date"] <= as_of]


def compute_holdings_at(as_of: str) -> dict[str, float]:
    """
    Compute {ticker: amount} by replaying all transactions up to as_of.
    Returns only non-zero holdings (|amount| > 1e-9).
    """
    balance: dict[str, dict] = {}
    for rec in get_all_transactions():
        if rec["date"] > as_of:
            break
        _apply_entries(balance, rec["entries"])
    return {k: v["amount"] for k, v in balance.items() if abs(v["amount"]) > 1e-9}


def get_tickers(include_cash: bool = False) -> set[str]:
    """Return all unique non-cash tickers present in the ledger."""
    tickers: set[str] = set()
    for rec in get_all_transactions():
        for e in rec["entries"]:
            t = e["ticker"].upper()
            if include_cash or t not in storage.SUPPORTED_CURRENCIES:
                tickers.add(t)
    return tickers


def get_all_tickers(include_fx: bool = True) -> set[str]:
    """Return all tickers including FX pairs needed for price data."""
    tickers: set[str] = set()
    cash_currencies: set[str] = set()
    for rec in get_all_transactions():
        for e in rec["entries"]:
            t = e["ticker"].upper()
            if t in storage.SUPPORTED_CURRENCIES:
                cash_currencies.add(t)
            else:
                tickers.add(t)
    if include_fx:
        if "USD" in cash_currencies or tickers:
            tickers.add("USDPLN")
        if "EUR" in cash_currencies:
            tickers.add("EURPLN")
            tickers.add("EURUSD")
        for ccy, suffixes in storage.CURRENCY_SUFFIXES.items():
            if ccy == "PLN":
                continue
            if not any(t.upper().endswith(s) for t in tickers for s in suffixes):
                continue
            if ccy in storage.TRIANGULATE_VIA_USD:
                tickers.add(f"{ccy}USD")
            else:
                tickers.add(f"{ccy}PLN")
    return tickers


def existing_entry_counts() -> dict[tuple[str, str, float], int]:
    """Return {(date, ticker, amount): occurrence_count} from the ledger.

    Multiset semantics for import dedup: an incoming entry is a duplicate
    only while the ledger already holds an unconsumed identical occurrence.
    Re-importing the same statement stays idempotent, while legitimate
    same-day same-quantity trades are no longer silently dropped (the old
    set-based key collapsed them).
    """
    counts: dict[tuple[str, str, float], int] = {}
    for rec in get_all_transactions():
        for e in rec["entries"]:
            key = (rec["date"], e["ticker"].upper(), round(float(e["amount"]), 8))
            counts[key] = counts.get(key, 0) + 1
    return counts


def get_ticker_history(ticker: str) -> list[dict]:
    """Return chronological buy/sell history for a single ticker.

    Scans the full ledger and returns one dict per matching entry:
      {date, amount, side, account_operation, running}
    where:
      - amount > 0 = buy, < 0 = sell
      - side is "Buy" / "Sell"
      - running is the cumulative position after this trade
    Cash / supported-currency entries are excluded.
    """
    ticker = ticker.upper()
    results: list[dict] = []
    running = 0.0
    for rec in get_all_transactions():
        for e in rec["entries"]:
            if e["ticker"].upper() != ticker:
                continue
            if ticker in storage.SUPPORTED_CURRENCIES:
                continue
            amt = float(e["amount"])
            if abs(amt) < 1e-12:
                continue
            running += amt
            results.append({
                "date": rec["date"],
                "amount": amt,
                "side": "Buy" if amt > 0 else "Sell",
                "account_operation": bool(e.get("account_operation", False)),
                "running": running,
            })
    return results


def get_ticker_legs(ticker: str) -> list[dict]:
    """Return the full set of ledger entries ("legs") for each trade involving ticker.

    Each returned dict groups one date's entries:
      {date, legs: [{ticker, amount, account_operation}, ...]}
    where "legs" includes ALL entries on that date (cash + any co-traded
    tickers), so the user can see e.g. "+10 AAPL @ $150 / -1500 USD" as one
    trade instead of only the AAPL row.
    """
    ticker = ticker.upper()
    results: list[dict] = []
    for rec in get_all_transactions():
        has_ticker = any(e["ticker"].upper() == ticker for e in rec["entries"])
        if not has_ticker:
            continue
        legs = [{
            "ticker": e["ticker"].upper(),
            "amount": round(float(e["amount"]), 8),
            "account_operation": bool(e.get("account_operation", False)),
        } for e in rec["entries"] if abs(float(e["amount"])) > 1e-12]
        results.append({"date": rec["date"], "legs": legs})
    return results


def find_negative_positions(
    transactions: list[dict],
    starting_balance: dict[str, float] | None = None,
) -> list[tuple[str, float, str]]:
    """Detect stock/ETF positions that would end negative after import.

    Returns a list of (ticker, missing_shares, first_negative_date) for each
    ticker that goes negative. This typically happens after corporate actions
    (splits, spin-offs) where the broker statement doesn't record the
    acquisition of the new shares — e.g. SYNEKTIK split into SYNEKTIK +
    SYN2BIO, and SYN2BIO was sold without a corresponding buy.

    Pass starting_balance to include existing ledger holdings in the check.
    """
    balance: dict[str, float] = dict(starting_balance) if starting_balance else {}
    for rec in transactions:
        for e in rec["entries"]:
            ticker = e["ticker"].upper()
            if ticker in storage.SUPPORTED_CURRENCIES:
                continue
            balance[ticker] = balance.get(ticker, 0.0) + float(e["amount"])

    negatives: list[tuple[str, float, str]] = []
    for ticker, amt in balance.items():
        if amt < -1e-9:
            first_neg_date = "2000-01-01"
            running: dict[str, float] = {}
            for rec in transactions:
                for e in rec["entries"]:
                    t = e["ticker"].upper()
                    if t in storage.SUPPORTED_CURRENCIES:
                        continue
                    running[t] = running.get(t, 0.0) + float(e["amount"])
                if running.get(ticker, 0.0) < -1e-9:
                    first_neg_date = rec["date"]
                    break
            negatives.append((ticker, round(abs(amt), 8), first_neg_date))
    return negatives


def auto_fix_negative_positions(
    transactions: list[dict],
    starting_balance: dict[str, float] | None = None,
) -> list[tuple[str, float, str]]:
    """Auto-insert zero-cost buys for positions that would end negative.

    Handles corporate actions (splits, spin-offs) where the broker statement
    doesn't record the acquisition of new shares. Inserts a buy at zero cost
    on the date the position first went negative — no cash leg is fabricated.

    Pass starting_balance to include existing ledger holdings in the check.
    This prevents double-fixing when the user has already manually added
    a corporate-action entry.

    Returns the list of (ticker, shares, date) that were auto-fixed, so the
    caller can log or display them. The user can later adjust the cost basis
    if they want to allocate from the parent stock.
    """
    negatives = find_negative_positions(transactions, starting_balance=starting_balance)
    if not negatives:
        return []

    for ticker, missing, first_neg_date in negatives:
        log.warning(
            "Corporate action detected: %s has %s shares with no buy — "
            "auto-inserting zero-cost acquisition on %s. "
            "Adjust cost basis later if needed.",
            ticker, missing, first_neg_date,
        )
        transactions.append({
            "date": first_neg_date,
            "entries": [{"ticker": ticker, "amount": missing}],
        })

    return negatives


def auto_fix_splits_if_needed(
    transactions: list[dict],
    starting_balance: dict[str, float] | None = None,
) -> list[tuple[str, str, float, float]]:
    """Run split auto-fix and log any corrections. Shared by all importers."""
    fixes = auto_fix_splits(transactions, starting_balance=starting_balance)
    if fixes:
        log.info(
            "Auto-fixed %d unapplied split(s): %s",
            len(fixes),
            ", ".join(f"{t} {d} {r}:1 (+{s:.4f})" for t, d, r, s in fixes),
        )
    return fixes


def auto_fix_splits(
    transactions: list[dict],
    starting_balance: dict[str, float] | None = None,
) -> list[tuple[str, str, float, float]]:
    """Auto-insert zero-cost buys for unapplied stock splits detected via Yahoo Finance.

    For each held ticker, fetches split history from Yahoo and checks whether
    the ledger reflects the post-split quantity. If not, inserts a zero-cost
    buy on the split date for the missing shares.

    Pass starting_balance to include existing ledger holdings.

    Returns list of (ticker, split_date, split_ratio, shares_added).
    """
    from ticker_data import get_splits

    all_tickers: set[str] = set()
    for rec in transactions:
        for e in rec["entries"]:
            t = e["ticker"].upper()
            if t not in storage.SUPPORTED_CURRENCIES:
                all_tickers.add(t)

    if not all_tickers:
        return []

    first_tx_date = min(rec["date"] for rec in transactions)

    fixes: list[tuple[str, str, float, float]] = []

    for ticker in sorted(all_tickers):
        splits = get_splits(ticker)
        if not splits:
            continue

        for split_date, ratio in sorted(splits.items()):
            if ratio <= 1.0:
                continue

            if split_date < first_tx_date:
                continue

            balance_before = (starting_balance or {}).get(ticker, 0.0)
            for rec in transactions:
                if rec["date"] >= split_date:
                    break
                for e in rec["entries"]:
                    if e["ticker"].upper() == ticker:
                        balance_before += float(e["amount"])

            if balance_before <= 1e-9:
                continue

            expected_after = balance_before * ratio

            balance_after = balance_before
            for rec in transactions:
                if rec["date"] < split_date:
                    continue
                if rec["date"] > split_date:
                    break
                for e in rec["entries"]:
                    if e["ticker"].upper() == ticker:
                        balance_after += float(e["amount"])

            missing = expected_after - balance_after
            if missing > 1e-9:
                # Skip if a fix for this (ticker, split_date) already exists
                # in the ledger (not just in the current transaction batch).
                from storage import SUPPORTED_CURRENCIES as _SC
                existing_txs = get_all_transactions()
                already_fixed = any(
                    e["ticker"].upper() == ticker
                    for rec in existing_txs
                    if rec["date"] == split_date
                    for e in rec["entries"]
                )
                if already_fixed:
                    continue

                log.warning(
                    "Split %s %s %s:1 not reflected in ledger (%s → expected %s, "
                    "got %s). Auto-inserting zero-cost buy for %s shares.",
                    ticker, split_date, ratio, ratio, expected_after, balance_after, missing,
                )
                transactions.append({
                    "date": split_date,
                    "entries": [{"ticker": ticker, "amount": missing}],
                })
                fixes.append((ticker, split_date, ratio, missing))

    return fixes
