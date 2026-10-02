"""Transaction ledger & metrics — pytest suite (split from the original monolithic runner)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch
import fixtures as fx
import json


def test_first_transaction_date(tmp: Path):
    """first_transaction_date → earliest tx date; None when there are none."""
    import ledger_core
    import storage
    from datetime import date as _date

    ledger_core.get_all_transactions._cache.clear()
    assert ledger_core.first_transaction_date() is None

    storage.transactions_path().parent.mkdir(parents=True, exist_ok=True)
    storage.transactions_path().write_bytes(
        b'{"date":"2024-03-05","entries":[]}\n'
        b'{"date":"2024-01-10","entries":[]}\n'
    )
    ledger_core.get_all_transactions._cache.clear()
    assert ledger_core.first_transaction_date() == _date(2024, 1, 10)
    ledger_core.get_all_transactions._cache.clear()


def test_first_portfolio_date_includes_inferred_manual_bond_purchase(tmp: Path):
    import bonds, ledger_core, storage

    storage.create_project("bond_start")
    ledger_core.add_transaction("2024-10-21", [{
        "ticker": "PLN", "amount": 1000.0, "account_operation": True,
    }])
    storage.save_bond_holdings([{
        "ticker": "EDO1233",
        "maturity_date": "2033-12-13",
        "units": 100,
    }])
    definition = bonds.BondDefinition("EDO1233", date(2023, 12, 1), 10, 0.05, 0.015)

    with patch("bonds.load_bond_definition", return_value=definition):
        assert ledger_core.first_portfolio_date() == date(2023, 12, 13)


def test_transaction_cache_is_scoped_to_project_with_identical_mtimes(tmp: Path):
    import os
    import ledger_core, storage

    storage.create_project("portfolio_a")
    path_a = storage.transactions_path()
    storage.write_jsonl(path_a, [{
        "date": "2025-01-01",
        "entries": [{"ticker": "NVDA", "amount": 2.0}],
    }])
    shared_mtime_ns = path_a.stat().st_mtime_ns

    storage.create_project("portfolio_b")
    path_b = storage.transactions_path()
    storage.write_jsonl(path_b, [{
        "date": "2025-01-01",
        "entries": [{"ticker": "NVDA", "amount": 7.0}],
    }])
    os.utime(path_b, ns=(shared_mtime_ns, shared_mtime_ns))

    ledger_core.get_all_transactions._cache.clear()
    storage.set_current_project("portfolio_a")
    transactions_a = ledger_core.get_all_transactions()
    storage.set_current_project("portfolio_b")
    transactions_b = ledger_core.get_all_transactions()

    assert transactions_a[0]["entries"][0]["amount"] == 2.0
    assert transactions_b[0]["entries"][0]["amount"] == 7.0


def test_manual_bond_flow_skips_units_already_in_ledger(tmp: Path):
    import bonds, ledger_core, storage

    definition = bonds.BondDefinition("EDO0127", date(2017, 1, 1), 10, 0.05, 0.015)
    storage.save_bond_holdings([
        {"ticker": "EDO0127", "maturity_date": "2037-01-01", "units": 1},
        {"ticker": "EDO0227", "maturity_date": "2037-02-01", "units": 2},
    ])
    records = [{
        "date": "2026-12-31",
        "entries": [{"ticker": "EDO0127", "amount": 1}],
    }]

    with patch("bonds.load_bond_definition", return_value=definition):
        flows = ledger_core._manual_bond_flows(records)

    assert flows == [("2027-02-01", 200.0)]


def test_add_transaction_simple(tmp: Path):
    """Adding a transaction creates the ledger and updates balance."""
    import ledger_core, storage
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.70},
    ])

    recs = ledger_core.get_all_transactions()
    assert len(recs) == 1
    assert recs[0]["date"] == "2023-01-03"

    bal = storage.load_balance()
    assert bal["AAPL"]["amount"] == 10.0
    assert abs(bal["USD"]["amount"] - (-1250.70)) < 0.01


def test_add_transaction_same_date_merges(tmp: Path):
    """Two transactions on the same date are merged into one line."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [{"ticker": "AAPL", "amount": 5.0}])
    ledger_core.add_transaction("2023-01-03", [{"ticker": "MSFT", "amount": 3.0}])

    recs = ledger_core.get_all_transactions()
    assert len(recs) == 1, "Same-date transactions should merge into one record"
    tickers_in_rec = {e["ticker"] for e in recs[0]["entries"]}
    assert "AAPL" in tickers_in_rec
    assert "MSFT" in tickers_in_rec


def test_add_transaction_chronological_append(tmp: Path):
    """Transactions on later dates are appended in order."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [{"ticker": "AAPL", "amount": 5.0}])
    ledger_core.add_transaction("2023-01-09", [{"ticker": "MSFT", "amount": 2.0}])
    ledger_core.add_transaction("2023-06-01", [{"ticker": "PLN", "amount": 1000.0}])

    recs = ledger_core.get_all_transactions()
    assert len(recs) == 3
    assert recs[0]["date"] == "2023-01-03"
    assert recs[1]["date"] == "2023-01-09"
    assert recs[2]["date"] == "2023-06-01"


def test_add_transaction_past_date_inserts_correctly(tmp: Path):
    """Inserting a past-date transaction reorders the file correctly."""
    import ledger_core

    ledger_core.add_transaction("2023-01-09", [{"ticker": "AAPL", "amount": 10.0}])
    ledger_core.add_transaction("2023-06-01", [{"ticker": "PLN", "amount": 500.0}])

    # Now insert something between them
    ledger_core.add_transaction("2023-01-04", [{"ticker": "USD", "amount": 1000.0}])

    recs = ledger_core.get_all_transactions()
    dates = [r["date"] for r in recs]
    assert dates == sorted(dates), f"Ledger must stay chronological, got: {dates}"
    assert dates[0] == "2023-01-04"


def test_balance_after_backdated_insert(tmp: Path):
    """REGRESSION: back-dated insert must not double-count existing records.

    The old _rebuild_balance(from_date=...) replayed the ledger suffix on top
    of a balance that already included those records, corrupting balance.json.
    """
    import ledger_core, storage

    ledger_core.add_transaction("2024-01-10", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1700.0},
    ])
    ledger_core.add_transaction("2023-12-01", [
        {"ticker": "AAPL", "amount": 5.0},
        {"ticker": "USD", "amount": -850.0},
    ])

    bal = storage.load_balance()
    assert bal["AAPL"]["amount"] == 15.0, \
        f"AAPL must be 15.0 after back-dated insert, got {bal['AAPL']['amount']}"
    assert bal["USD"]["amount"] == -2550.0, \
        f"USD must be -2550.0 after back-dated insert, got {bal['USD']['amount']}"


def test_balance_after_same_date_merge(tmp: Path):
    """REGRESSION: merging into an existing date must not re-apply old entries."""
    import ledger_core, storage

    # Merge into the LAST date
    ledger_core.add_transaction("2024-01-10", [
        {"ticker": "AAPL", "amount": 10.0}, {"ticker": "USD", "amount": -1700.0},
    ])
    ledger_core.add_transaction("2024-01-10", [
        {"ticker": "MSFT", "amount": 2.0}, {"ticker": "USD", "amount": -700.0},
    ])
    bal = storage.load_balance()
    assert bal["AAPL"]["amount"] == 10.0, \
        f"AAPL must be 10.0 after last-date merge, got {bal['AAPL']['amount']}"
    assert bal["MSFT"]["amount"] == 2.0
    assert bal["USD"]["amount"] == -2400.0

    # Merge into an EARLIER date
    ledger_core.add_transaction("2023-12-01", [
        {"ticker": "AAPL", "amount": 5.0}, {"ticker": "USD", "amount": -850.0},
    ])
    bal = storage.load_balance()
    assert bal["AAPL"]["amount"] == 15.0, \
        f"AAPL must be 15.0 after past-date merge, got {bal['AAPL']['amount']}"
    assert bal["USD"]["amount"] == -3250.0


def test_existing_entry_counts_allows_legit_duplicates(tmp: Path):
    """Multiset dedup: two identical same-day buys must both importable."""
    import ledger_core, storage

    ledger_core.add_transaction("2024-01-10", [{"ticker": "AAPL", "amount": 5.0}])
    counts = ledger_core.existing_entry_counts()

    key = ("2024-01-10", "AAPL", 5.0)
    assert counts.get(key, 0) == 1

    # Simulate importing a statement that contains the same buy twice:
    # first occurrence is a duplicate, second is legitimate.
    first_is_dup = counts.get(key, 0) > 0
    if first_is_dup:
        counts[key] -= 1
    second_is_dup = counts.get(key, 0) > 0
    assert first_is_dup and not second_is_dup, \
        "multiset dedup must allow one additional identical entry"


def test_compute_holdings_at(tmp: Path):
    """compute_holdings_at returns correct balances at a given date."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [{"ticker": "AAPL", "amount": 10.0}])
    ledger_core.add_transaction("2023-06-01", [{"ticker": "AAPL", "amount": -5.0}])

    holdings_jan = ledger_core.compute_holdings_at("2023-01-31")
    assert holdings_jan["AAPL"] == 10.0

    holdings_jun = ledger_core.compute_holdings_at("2023-12-31")
    assert holdings_jun["AAPL"] == 5.0


def test_balance_after_full_sell(tmp: Path):
    """Selling all shares of a ticker removes it from holdings."""
    import ledger_core, storage

    ledger_core.add_transaction("2023-01-03", [{"ticker": "AAPL", "amount": 10.0}])
    ledger_core.add_transaction("2023-01-09", [{"ticker": "AAPL", "amount": -10.0}])

    bal = storage.load_balance()
    assert "AAPL" not in bal or abs(bal.get("AAPL", {}).get("amount", 0)) < 1e-6, \
        "After full sell, AAPL should be gone from balance"


def test_get_tickers(tmp: Path):
    """get_tickers returns all non-cash tickers from the ledger."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},  # cash — should be excluded
    ])
    ledger_core.add_transaction("2023-01-09", [
        {"ticker": "CDR.WA", "amount": 5.0},
        {"ticker": "PLN", "amount": -650.0},   # cash — excluded
    ])

    tickers = ledger_core.get_tickers(include_cash=False)
    assert "AAPL" in tickers
    assert "CDR.WA" in tickers
    assert "USD" not in tickers
    assert "PLN" not in tickers


def test_buy_eur_etf_full_sell(tmp: Path):
    """Buy QDVE.DE (EUR ETF), sell all shares, receive EUR back."""
    import ledger_core, storage

    fx.inject_fake_prices(tmp)
    storage.save_price_year("QDVE.DE", 2023, {
        "2023-01-03": 200.0,
        "2023-06-01": 240.0,
    })

    # Buy: 5 shares at 200 EUR each
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "QDVE.DE", "amount": 5.0},
        {"ticker": "EUR",     "amount": -1000.0},
    ])
    # Sell: all 5 shares at 240 EUR each = 1200 EUR proceeds
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "QDVE.DE", "amount": -5.0},
        {"ticker": "EUR",     "amount": 1200.0},
    ])

    bal = storage.load_balance()
    assert "QDVE.DE" not in bal or abs(bal.get("QDVE.DE", {}).get("amount", 0)) < 1e-9, \
        "QDVE.DE should be fully sold"
    assert abs(bal.get("EUR", {}).get("amount", 0) - 200.0) < 0.01, \
        f"Expected 200 EUR profit remaining, got {bal.get('EUR', {}).get('amount', 0)}"


def test_buy_usd_stock_partial_sell(tmp: Path):
    """Buy GOOG in USD, partially sell, verify correct remaining balance."""
    import ledger_core, storage

    fx.inject_fake_prices(tmp)
    storage.save_price_year("GOOG", 2023, {
        "2023-01-03": 88.0,
        "2023-06-01": 122.0,
    })

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "GOOG", "amount": 10.0},
        {"ticker": "USD",  "amount": -880.0},
    ])
    # Sell 4 of 10 shares
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "GOOG", "amount": -4.0},
        {"ticker": "USD",  "amount": 488.0},   # 4 × 122
    ])

    bal = storage.load_balance()
    assert abs(bal.get("GOOG", {}).get("amount", 0) - 6.0) < 1e-9, \
        f"Expected 6 GOOG remaining, got {bal.get('GOOG', {}).get('amount', 0)}"
    assert abs(bal.get("USD", {}).get("amount", 0) - (-392.0)) < 0.01, \
        f"Expected -392 USD (net cash spent), got {bal.get('USD', {}).get('amount', 0)}"


def test_delete_transaction(tmp: Path):
    """Deleting one entry removes it and rebuilds balance."""
    import ledger_core, storage

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.70},
    ])

    ledger_core.delete_transaction("2023-01-03", 0)

    recs = ledger_core.get_all_transactions()
    assert len(recs) == 1
    assert len(recs[0]["entries"]) == 1
    assert recs[0]["entries"][0]["ticker"] == "USD"

    bal = storage.load_balance()
    assert "AAPL" not in bal or abs(bal.get("AAPL", {}).get("amount", 0)) < 1e-6


def test_delete_last_entry_removes_record(tmp: Path):
    """Deleting the only entry in a date removes the entire record."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [{"ticker": "AAPL", "amount": 10.0}])
    ledger_core.add_transaction("2023-06-01", [{"ticker": "MSFT", "amount": 5.0}])

    ledger_core.delete_transaction("2023-01-03", 0)

    recs = ledger_core.get_all_transactions()
    assert len(recs) == 1
    assert recs[0]["date"] == "2023-06-01"


def test_update_transaction(tmp: Path):
    """Updating an entry changes ticker, amount, and account_operation."""
    import ledger_core, storage

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.70},
    ])

    ledger_core.update_transaction("2023-01-03", 0, "MSFT", 20.0, account_operation=True)

    recs = ledger_core.get_all_transactions()
    e = recs[0]["entries"][0]
    assert e["ticker"] == "MSFT"
    assert e["amount"] == 20.0
    assert e.get("account_operation") is True

    bal = storage.load_balance()
    assert bal["MSFT"]["amount"] == 20.0
    assert "AAPL" not in bal or abs(bal.get("AAPL", {}).get("amount", 0)) < 1e-6


def test_set_account_operation(tmp: Path):
    """set_account_operation toggles the flag on an entry."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},
    ])

    recs = ledger_core.get_all_transactions()
    assert recs[0]["entries"][0].get("account_operation") is None

    ledger_core.set_account_operation("2023-01-03", 0, True)
    recs = ledger_core.get_all_transactions()
    assert recs[0]["entries"][0].get("account_operation") is True

    ledger_core.set_account_operation("2023-01-03", 0, False)
    recs = ledger_core.get_all_transactions()
    assert recs[0]["entries"][0].get("account_operation") is None


def test_get_transactions_up_to(tmp: Path):
    """get_transactions_up_to returns only transactions up to the given date."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [{"ticker": "AAPL", "amount": 5.0}])
    ledger_core.add_transaction("2023-01-10", [{"ticker": "MSFT", "amount": 3.0}])
    ledger_core.add_transaction("2023-06-01", [{"ticker": "GOOG", "amount": 2.0}])

    result = ledger_core.get_transactions_up_to("2023-01-10")
    dates = [r["date"] for r in result]
    assert "2023-01-03" in dates
    assert "2023-01-10" in dates
    assert "2023-06-01" not in dates
    assert len(result) == 2


def test_get_all_tickers(tmp: Path):
    """get_all_tickers returns stock tickers plus FX pair tickers."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},
    ])
    ledger_core.add_transaction("2023-01-06", [
        {"ticker": "QDVE.DE", "amount": 5.0},
        {"ticker": "EUR", "amount": -1000.0},
    ])

    tickers = ledger_core.get_all_tickers(include_fx=True)
    assert "AAPL" in tickers
    assert "QDVE.DE" in tickers
    # Cash tickers excluded, FX pairs included
    assert "USD" not in tickers
    assert "EUR" not in tickers


def test_rebuild_balance_includes_today_transactions(tmp: Path):
    """_rebuild_balance includes today's records in balance and avg_price."""
    from unittest.mock import patch
    import ledger_core, storage
    from datetime import date as _real_date

    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
    ])
    ledger_core.add_transaction("2023-01-05", [
        {"ticker": "AAPL", "amount": 5.0},
    ])

    class _FakeDate(_real_date):
        @classmethod
        def today(cls):
            return _real_date(2023, 1, 5)

    with patch.object(ledger_core, "date", _FakeDate):
        records = ledger_core.get_all_transactions()
        ledger_core._rebuild_balance(records)

    bal = storage.load_balance()
    assert abs(bal["AAPL"]["amount"] - 15.0) < 1e-6, \
        f"Balance should be 15 AAPL (today's +5 included), got {bal['AAPL']['amount']}"


def test_avg_price_stored_in_native_currency(tmp: Path):
    """avg_price is stored in the ticker's native currency (EUR for .DE stocks)."""
    import ledger_core, storage

    fx.inject_fake_prices(tmp)
    storage.save_price_year("SEC0.DE", 2023, {"2023-01-03": 88.27})

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "SEC0.DE", "amount": 288.0},
        {"ticker": "EUR",     "amount": -25425.0},
    ])

    bal = storage.load_balance()
    avg = bal["SEC0.DE"]["avg_price"]
    assert abs(avg - 88.27) < 0.01, \
        f"avg_price should be 88.27 EUR (native), got {avg}"


def test_avg_price_usd_stored_in_usd(tmp: Path):
    """avg_price for USD stock is stored in USD."""
    import ledger_core, storage

    fx.inject_fake_prices(tmp)
    storage.save_price_year("GOOG", 2023, {"2023-01-03": 88.0})

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "GOOG", "amount": 10.0},
        {"ticker": "USD",  "amount": -880.0},
    ])

    bal = storage.load_balance()
    avg = bal["GOOG"]["avg_price"]
    assert abs(avg - 88.0) < 0.01, \
        f"avg_price should be 88.0 USD (native), got {avg}"


def test_get_ticker_history_buys_and_sells(tmp: Path):
    """get_ticker_history returns chronological buys/sells with correct sides."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD",  "amount": -1250.0},
    ])
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "AAPL", "amount": -4.0},
        {"ticker": "USD",  "amount": 500.0},
    ])

    hist = ledger_core.get_ticker_history("AAPL")
    assert len(hist) == 2
    assert hist[0]["side"] == "Buy"
    assert hist[0]["amount"] == 10.0
    assert hist[0]["date"] == "2023-01-03"
    assert hist[1]["side"] == "Sell"
    assert hist[1]["amount"] == -4.0
    assert hist[1]["date"] == "2023-06-01"


def test_get_ticker_history_running_shares(tmp: Path):
    """Running position tracks cumulative shares across trades."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [{"ticker": "GOOG", "amount": 10.0}])
    ledger_core.add_transaction("2023-03-01", [{"ticker": "GOOG", "amount": 5.0}])
    ledger_core.add_transaction("2023-06-01", [{"ticker": "GOOG", "amount": -8.0}])

    hist = ledger_core.get_ticker_history("GOOG")
    assert len(hist) == 3
    assert hist[0]["running"] == 10.0
    assert hist[1]["running"] == 15.0
    assert hist[2]["running"] == 7.0


def test_get_ticker_history_excludes_cash(tmp: Path):
    """Cash tickers (USD, EUR, PLN) are excluded from history."""
    import ledger_core

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD",  "amount": -1250.0},
    ])

    hist_aapl = ledger_core.get_ticker_history("AAPL")
    assert len(hist_aapl) == 1

    hist_usd = ledger_core.get_ticker_history("USD")
    assert len(hist_usd) == 0

    hist_pln = ledger_core.get_ticker_history("PLN")
    assert len(hist_pln) == 0


def test_twr_basic(tmp: Path):
    """TWR: single deposit then growth — cumulative return chains correctly."""
    import ledger_core
    from ledger_core import annualize_twr, compute_twr
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])

    snapshots = [
        {"date": "2023-01-03", "total_value": 10000.0},
        {"date": "2023-07-03", "total_value": 11000.0},
    ]

    twr = compute_twr(snapshots, "PLN")
    assert twr is not None
    assert abs(twr - 0.10) < 1e-9

    ann = annualize_twr(twr, (date(2023, 7, 3) - date(2023, 1, 3)).days)
    assert ann > 0.10  # a positive cumulative return compounds when annualized


def test_twr_no_snapshots():
    """TWR: empty snapshot list returns None."""
    from ledger_core import compute_twr
    assert compute_twr([], "PLN") is None


def test_twr_single_snapshot(tmp: Path):
    """TWR: one snapshot — no measurable period, returns None."""
    import ledger_core
    from ledger_core import compute_twr
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])

    assert compute_twr([{"date": "2023-01-03", "total_value": 10000.0}], "PLN") is None


def test_twr_with_withdrawal(tmp: Path):
    """TWR: withdrawal is an external flow, not a market loss."""
    import ledger_core
    from ledger_core import compute_twr
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-07-03", [
        {"ticker": "PLN", "amount": -10000.0, "account_operation": True},
    ])

    snapshots = [
        {"date": "2023-01-03", "total_value": 10000.0},
        {"date": "2023-07-02", "total_value": 11000.0},
        {"date": "2023-07-03", "total_value": 1200.0},
    ]

    # d2 factor: 11000/10000 = 1.1; d3 factor: (1200 + 10000)/11000 = 1.01818…
    twr = compute_twr(snapshots, "PLN")
    assert twr is not None
    assert abs(twr - (1.1 * (1200.0 + 10000.0) / 11000.0 - 1.0)) < 1e-9


def test_twr_zero_value_gap(tmp: Path):
    """TWR: full withdrawal then fresh deposit — no phantom -100%/+100%."""
    import ledger_core
    from ledger_core import compute_twr
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-01-04", [
        {"ticker": "PLN", "amount": -10000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-01-05", [
        {"ticker": "PLN", "amount": 5000.0, "account_operation": True},
    ])

    snapshots = [
        {"date": "2023-01-03", "total_value": 10000.0},
        {"date": "2023-01-04", "total_value": 0.0},
        {"date": "2023-01-05", "total_value": 5000.0},
        {"date": "2023-01-06", "total_value": 5500.0},
    ]

    # d2 factor: (0 + 10000)/10000 = 1.0; d3 skipped (prev value 0);
    # d4 factor: 5500/5000 = 1.1 → cumulative 10%.
    twr = compute_twr(snapshots, "PLN")
    assert twr is not None
    assert abs(twr - 0.10) < 1e-9


def test_twr_with_fx(tmp: Path):
    """TWR: USD deposit converted to PLN at the flow-date FX rate."""
    import ledger_core
    from ledger_core import compute_twr
    fx.inject_fake_prices(tmp)

    # 2023-01-04 USDPLN = 4.37 → flow = 4370 PLN.
    ledger_core.add_transaction("2023-01-04", [
        {"ticker": "USD", "amount": 1000.0, "account_operation": True},
    ])

    snapshots = [
        {"date": "2023-01-03", "total_value": 5000.0},
        {"date": "2023-01-04", "total_value": 10430.0},
    ]

    # d1 skipped (first snapshot); d2 factor: (10430 - 4370)/5000 = 1.212
    twr = compute_twr(snapshots, "PLN")
    assert twr is not None
    assert abs(twr - 0.212) < 1e-9


def test_twr_end_cutoff(tmp: Path):
    """TWR: ``end`` excludes later snapshots and flows."""
    import ledger_core
    from ledger_core import compute_twr
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-01-06", [
        {"ticker": "PLN", "amount": 5000.0, "account_operation": True},
    ])

    snapshots = [
        {"date": "2023-01-03", "total_value": 10000.0},
        {"date": "2023-01-04", "total_value": 10500.0},
        {"date": "2023-01-05", "total_value": 11000.0},
        {"date": "2023-01-06", "total_value": 16600.0},
    ]

    # end=2023-01-05: only d2 and d3 chain → 1.05 * (11000/10500) - 1.
    twr = compute_twr(snapshots, "PLN", end="2023-01-05")
    assert twr is not None
    assert abs(twr - (1.05 * (11000.0 / 10500.0) - 1.0)) < 1e-9


def test_irr_basic(tmp: Path):
    """IRR: single deposit with positive return yields positive IRR."""
    import ledger_core
    from ledger_core import compute_irr
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])

    irr = compute_irr(12000.0, "PLN")
    assert irr is not None
    assert irr > 0


def test_irr_no_transactions(tmp: Path):
    """IRR: no transactions returns None."""
    from ledger_core import compute_irr
    assert compute_irr(10000.0, "PLN") is None


def test_irr_multiple_deposits(tmp: Path):
    """IRR: multiple deposits are handled correctly."""
    import ledger_core
    from ledger_core import compute_irr
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 5000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "PLN", "amount": 5000.0, "account_operation": True},
    ])

    irr = compute_irr(12000.0, "PLN")
    assert irr is not None
    assert irr > 0


def test_irr_with_withdrawal(tmp: Path):
    """IRR: withdrawal reduces invested capital correctly."""
    import ledger_core
    from ledger_core import compute_irr
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "PLN", "amount": -3000.0, "account_operation": True},
    ])

    irr = compute_irr(9000.0, "PLN")
    assert irr is not None


def test_irr_loss(tmp: Path):
    """IRR: value below invested yields negative IRR."""
    import ledger_core
    from ledger_core import compute_irr
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])

    irr = compute_irr(5000.0, "PLN")
    assert irr is not None
    assert irr < 0


def _reference_irr(ext_flows: list, terminal: float) -> float:
    """Independent XIRR solver (bisection) used as an oracle for compute_irr.

    ``ext_flows`` is a list of (date_str, amount_in_base_ccy) where outflows
    are negative (deposits) and inflows positive (withdrawals). ``terminal``
    is the final positive portfolio value dated today.
    """
    from datetime import date as _d

    today = _d.today().isoformat()
    items = sorted(list(ext_flows) + [(today, terminal)], key=lambda x: x[0])
    start = _d.fromisoformat(items[0][0])
    yrs = [(_d.fromisoformat(d) - start).days / 365.25 for d, _ in items]
    amts = [a for _, a in items]

    def npv(r: float) -> float:
        return sum(a / (1.0 + r) ** t for a, t in zip(amts, yrs))

    lo, hi = -0.5, 5.0
    for _ in range(300):
        mid = (lo + hi) / 2.0
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
        if abs(hi - lo) < 1e-11:
            break
    return (lo + hi) / 2.0


def _rebuild_npv(current_value: float, base_ccy: str) -> float:
    """Rebuild compute_irr's cash flows and return NPV at the computed IRR.

    Oracle-free correctness check: the IRR returned by compute_irr must zero
    the NPV of exactly the flows it used.
    """
    from datetime import date as _d
    from ledger_core import compute_irr, get_all_transactions
    from ticker_data import get_fx_rate

    base = base_ccy.upper()
    flows: list = []
    for rec in get_all_transactions():
        for e in rec["entries"]:
            if not e.get("account_operation", False):
                continue
            t = e["ticker"].upper()
            amt = float(e["amount"])
            fx = get_fx_rate(t, base, rec["date"], {}, int(rec["date"][:4])) if t != base else 1.0
            flows.append((rec["date"], -amt * fx))

    today = _d.today().isoformat()
    flows.append((today, current_value))
    flows.sort(key=lambda x: x[0])
    start = _d.fromisoformat(flows[0][0])
    yrs = [(_d.fromisoformat(d) - start).days / 365.25 for d, _ in flows]
    amts = [a for _, a in flows]

    irr = compute_irr(current_value, base)
    return irr, sum(a / (1.0 + irr) ** t for a, t in zip(amts, yrs))


def test_irr_known_two_flow(tmp: Path):
    """IRR: a 1-year 1000→1100 deposit equals the closed-form ~10%."""
    import ledger_core
    from ledger_core import compute_irr
    from datetime import date as _d

    fx.inject_fake_prices(tmp)
    dep = _d.today().replace(year=_d.today().year - 1).isoformat()
    ledger_core.add_transaction(dep, [
        {"ticker": "PLN", "amount": 1000.0, "account_operation": True},
    ])
    irr = compute_irr(1100.0, "PLN")
    days = (_d.today() - _d.fromisoformat(dep)).days
    expected = (1100.0 / 1000.0) ** (365.25 / days) - 1.0
    assert abs(irr - expected) < 1e-6


def test_irr_dividend_ignored(tmp: Path):
    """IRR regression: a dividend must NOT change the IRR (it is internal)."""
    import ledger_core
    from ledger_core import compute_irr

    fx.inject_fake_prices(tmp)
    # baseline: deposit only
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])
    irr_base = compute_irr(12000.0, "PLN")

    # same deposit + a dividend (currency entry, no account_operation flag)
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "PLN", "amount": 100.0},
    ])
    irr_div = compute_irr(12000.0, "PLN")
    assert abs(irr_base - irr_div) < 1e-9


def test_irr_withholding_tax_ignored(tmp: Path):
    """IRR regression: a withholding-tax entry must NOT change the IRR."""
    import ledger_core
    from ledger_core import compute_irr

    fx.inject_fake_prices(tmp)
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])
    irr_base = compute_irr(12000.0, "PLN")
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "PLN", "amount": -20.0},
    ])
    irr_tax = compute_irr(12000.0, "PLN")
    assert abs(irr_base - irr_tax) < 1e-9


def test_irr_fx_deposit(tmp: Path):
    """IRR: a USD deposit is FX-converted and matches a PLN reference IRR."""
    import ledger_core
    from ledger_core import compute_irr
    from ticker_data import get_fx_rate

    fx.inject_fake_prices(tmp)
    dep_date = "2023-01-03"  # USDPLN = 4.38 in fake data
    usd_rate = get_fx_rate("USD", "PLN", dep_date, {}, 2023)
    ledger_core.add_transaction(dep_date, [
        {"ticker": "USD", "amount": 1000.0, "account_operation": True},
    ])
    # terminal value = deposit grown 10% in USD terms, expressed in PLN
    terminal = 1000.0 * usd_rate * 1.10
    irr = compute_irr(terminal, "PLN")
    ref = _reference_irr([(dep_date, -1000.0 * usd_rate)], terminal)
    assert abs(irr - ref) < 1e-6


def test_irr_matches_reference_xirr(tmp: Path):
    """IRR: solver agrees with an independent bisection XIRR oracle."""
    import ledger_core
    from ledger_core import compute_irr

    fx.inject_fake_prices(tmp)
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 5000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "PLN", "amount": -2000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-09-15", [
        {"ticker": "PLN", "amount": 3000.0, "account_operation": True},
    ])
    terminal = 9000.0
    flows = [
        ("2023-01-03", -5000.0),
        ("2023-06-01", 2000.0),
        ("2023-09-15", -3000.0),
    ]
    irr = compute_irr(terminal, "PLN")
    ref = _reference_irr(flows, terminal)
    assert abs(irr - ref) < 1e-6


def test_irr_npv_is_zero(tmp: Path):
    """IRR property: compute_irr's rate zeroes the NPV of its own flows."""
    import ledger_core
    from ledger_core import compute_irr

    fx.inject_fake_prices(tmp)
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 5000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "PLN", "amount": -2000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-09-15", [
        {"ticker": "PLN", "amount": 3000.0, "account_operation": True},
    ])
    _, npv = _rebuild_npv(9000.0, "PLN")
    assert abs(npv) < 1e-6


def test_irr_multiple_sign_changes(tmp: Path):
    """IRR: deposit/withdraw/deposit still yields a finite, bounded rate."""
    import ledger_core
    from ledger_core import compute_irr

    fx.inject_fake_prices(tmp)
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 1000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "PLN", "amount": -400.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-09-15", [
        {"ticker": "PLN", "amount": 600.0, "account_operation": True},
    ])
    irr = compute_irr(1500.0, "PLN")
    assert irr is not None
    assert -0.5 <= irr <= 5.0


def test_irr_high_return(tmp: Path):
    """IRR: a very large gain stays within the solver's upper bound."""
    import ledger_core
    from ledger_core import compute_irr

    fx.inject_fake_prices(tmp)
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 1000.0, "account_operation": True},
    ])
    irr = compute_irr(20000.0, "PLN")
    assert irr is not None
    assert irr > 0.5


def test_avg_price_sell_reduces_proportionally(tmp: Path):
    """_update_avg_prices: sell-only reduces cost pool proportionally."""
    import ledger_core
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.70},
    ])

    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "AAPL", "amount": -5.0},
        {"ticker": "USD", "amount": 900.45},
    ])

    import storage
    bal = storage.load_balance()
    assert bal["AAPL"]["amount"] == 5.0
    assert bal["AAPL"]["avg_price"] > 0


def test_avg_price_full_sell_zeroes(tmp: Path):
    """_update_avg_prices: full sell removes ticker from balance."""
    import ledger_core
    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.70},
    ])

    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "AAPL", "amount": -10.0},
        {"ticker": "USD", "amount": 1800.90},
    ])

    import storage
    bal = storage.load_balance()
    assert "AAPL" not in bal or abs(bal.get("AAPL", {}).get("amount", 0)) < 1e-9


def test_remap_tickers_applies_rules(tmp: Path):
    """remap_tickers re-applies ticker_rules to existing entries."""
    import ledger_core
    import storage
    import config

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.70},
    ])

    cfg = config.load()
    cfg["ticker_rules"] = ["AAPL=MSFT"]
    config.save(cfg)

    changed = ledger_core.remap_tickers()
    assert changed == 1

    records = storage.read_jsonl(storage.transactions_path())
    tickers = [e["ticker"] for r in records for e in r["entries"]]
    assert "MSFT" in tickers
    assert "AAPL" not in tickers


def test_remap_tickers_no_op(tmp: Path):
    """remap_tickers is a no-op when rules don't change any ticker."""
    import ledger_core
    import storage
    import config

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.70},
    ])

    cfg = config.load()
    cfg["ticker_rules"] = ["MSFT=AAPL"]
    config.save(cfg)

    changed = ledger_core.remap_tickers()
    assert changed == 0


# -- auto_fix_splits: detect and fix unapplied stock splits -----------------

def test_auto_fix_splits_inserts_missing_shares(tmp: Path, monkeypatch):
    """auto_fix_splits detects a 2:1 split and inserts the missing shares."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    # User holds 10 AAPL, a 2:1 split happens, user never recorded it
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},
    ])

    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {"2023-06-01": 2.0} if t == "AAPL" else {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    assert len(fixes) == 1
    ticker, split_date, ratio, shares_added = fixes[0]
    assert ticker == "AAPL"
    assert split_date == "2023-06-01"
    assert ratio == 2.0
    assert abs(shares_added - 10.0) < 1e-6


def test_auto_fix_splits_no_fix_when_already_applied(tmp: Path, monkeypatch):
    """auto_fix_splits does not double-fix when the split is already reflected."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    # User bought 10 AAPL, then manually added the split
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},
    ])
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "AAPL", "amount": 10.0},  # manual split entry
    ])

    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {"2023-06-01": 2.0} if t == "AAPL" else {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    assert len(fixes) == 0


def test_auto_fix_splits_ignores_cash_tickers(tmp: Path, monkeypatch):
    """auto_fix_splits does not process cash/currency tickers."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "USD", "amount": 5000.0, "account_operation": True},
    ])

    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    assert len(fixes) == 0


def test_auto_fix_splits_with_starting_balance(tmp: Path, monkeypatch):
    """auto_fix_splits accounts for existing ledger holdings via starting_balance.

    User has 10 AAPL pre-split (from starting_balance). A buy on 2023-01-03
    sets the first_tx_date before the split at 2023-03-01. The function
    detects the missing post-split shares.
    """
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    # User buys more AAPL before the split, establishing a tx before split_date
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 5.0},
        {"ticker": "USD", "amount": 625.0},
    ])
    # Then sells some after the split
    ledger_core.add_transaction("2023-07-01", [
        {"ticker": "AAPL", "amount": -5.0},
        {"ticker": "USD", "amount": 900.0},
    ])

    starting = {"AAPL": 10.0}
    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {"2023-03-01": 2.0} if t == "AAPL" else {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions, starting_balance=starting)

    assert len(fixes) == 1
    assert fixes[0][0] == "AAPL"
    assert fixes[0][2] == 2.0
    # balance_before = 10 (starting) + 5 (buy) = 15; expected_after = 30;
    # balance_after = 15 (no tx on split date); missing = 15
    assert abs(fixes[0][3] - 15.0) < 1e-6


def test_auto_fix_splits_skips_reverse_split(tmp: Path, monkeypatch):
    """auto_fix_splits ignores reverse splits (ratio <= 1.0)."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},
    ])

    # Reverse split (10:1 → ratio 0.1) and no-op (ratio 1.0)
    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {
        "2023-06-01": 0.1,
        "2023-07-01": 1.0,
    } if t == "AAPL" else {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    assert fixes == []


def test_auto_fix_splits_skips_split_before_first_tx(tmp: Path, monkeypatch):
    """auto_fix_splits ignores splits dated before the earliest transaction."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    # Transaction on 2023-06-01, but split happened 2022-01-01 (before any tx)
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},
    ])

    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {
        "2022-01-01": 2.0,
    } if t == "AAPL" else {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    assert fixes == []


def test_auto_fix_splits_skips_when_no_shares_held(tmp: Path, monkeypatch):
    """auto_fix_splits does not fix a split if position was zero before split."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    # Sell before split reduces position to zero, then split happens
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},
    ])
    ledger_core.add_transaction("2023-05-01", [
        {"ticker": "AAPL", "amount": -10.0},
        {"ticker": "USD", "amount": 1500.0},
    ])

    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {
        "2023-06-01": 2.0,
    } if t == "AAPL" else {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    assert fixes == []


def test_auto_fix_splits_multiple_tickers_independent(tmp: Path, monkeypatch):
    """auto_fix_splits processes multiple tickers with different splits."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "MSFT", "amount": 5.0},
        {"ticker": "USD", "amount": -2000.0},
    ])

    def mock_splits(t):
        return {
            "AAPL": {"2023-06-01": 2.0},
            "MSFT": {"2023-07-01": 3.0},
        }.get(t, {})

    monkeypatch.setattr(ticker_data, "get_splits", mock_splits)

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    assert len(fixes) == 2
    fix_by_ticker = {f[0]: f for f in fixes}
    assert fix_by_ticker["AAPL"][2] == 2.0
    assert abs(fix_by_ticker["AAPL"][3] - 10.0) < 1e-6
    assert fix_by_ticker["MSFT"][2] == 3.0
    assert abs(fix_by_ticker["MSFT"][3] - 10.0) < 1e-6  # 5 * 3 - 5 = 10


def test_auto_fix_splits_balance_after_fix(tmp: Path, monkeypatch):
    """Balance is correct after auto_fix_splits appends the fix entry."""
    import ledger_core
    import ticker_data
    import storage

    ledger_core.get_all_transactions._cache.clear()

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},
    ])

    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {
        "2023-06-01": 2.0,
    } if t == "AAPL" else {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)
    assert len(fixes) == 1

    # Append the fix entry to the ledger (as import_xtb would do)
    for fix_rec in transactions:
        if fix_rec["date"] == "2023-06-01" and any(
            e["ticker"] == "AAPL" for e in fix_rec["entries"]
        ):
            ledger_core.add_transaction(fix_rec["date"], fix_rec["entries"])

    bal = storage.load_balance()
    assert abs(bal["AAPL"]["amount"] - 20.0) < 1e-6  # 10 * 2 = 20


def test_auto_fix_splits_odd_ratio(tmp: Path, monkeypatch):
    """auto_fix_splits handles non-power-of-2 ratios correctly."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "XYZ", "amount": 6.0},
        {"ticker": "USD", "amount": -600.0},
    ])

    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {
        "2023-06-01": 5.0,
    } if t == "XYZ" else {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    assert len(fixes) == 1
    # balance_before = 6, expected_after = 30, missing = 24
    assert fixes[0][0] == "XYZ"
    assert abs(fixes[0][3] - 24.0) < 1e-6


def test_auto_fix_splits_sells_before_split(tmp: Path, monkeypatch):
    """Sells before split reduce balance_before, fixing the correct amount."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    # Buy 10, sell 4, then split: should fix 6*(ratio-1)
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.0},
    ])
    ledger_core.add_transaction("2023-04-01", [
        {"ticker": "AAPL", "amount": -4.0},
        {"ticker": "USD", "amount": 800.0},
    ])

    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {
        "2023-06-01": 2.0,
    } if t == "AAPL" else {})

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    assert len(fixes) == 1
    # balance_before = 10 - 4 = 6; expected_after = 12; missing = 6
    assert abs(fixes[0][3] - 6.0) < 1e-6


def test_auto_fix_splits_empty_transactions(tmp: Path, monkeypatch):
    """auto_fix_splits returns empty list for empty transactions."""
    import ledger_core
    import ticker_data

    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {"2023-06-01": 2.0})

    fixes = ledger_core.auto_fix_splits([])
    assert fixes == []


def test_auto_fix_splits_cross_ticker_dedup_independence(tmp: Path, monkeypatch):
    """A split fix for one ticker does not block fixes for other tickers."""
    import ledger_core
    import ticker_data

    ledger_core.get_all_transactions._cache.clear()

    # Both tickers have buys, both have splits
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "MSFT", "amount": 5.0},
        {"ticker": "USD", "amount": -2000.0},
    ])

    # Manually add a fix for AAPL on the split date
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "AAPL", "amount": 10.0},
    ])

    def mock_splits(t):
        return {
            "AAPL": {"2023-06-01": 2.0},
            "MSFT": {"2023-06-01": 2.0},
        }.get(t, {})

    monkeypatch.setattr(ticker_data, "get_splits", mock_splits)

    transactions = ledger_core.get_all_transactions()
    fixes = ledger_core.auto_fix_splits(transactions)

    # AAPL fix already exists → skipped; MSFT fix is new → applied
    assert len(fixes) == 1
    assert fixes[0][0] == "MSFT"
    assert abs(fixes[0][3] - 5.0) < 1e-6


# -- auto_fix_negative_positions: detect and fix unapplied corporate actions -

def test_auto_fix_negative_multi_ticker(tmp: Path):
    """auto_fix_negative_positions fixes two tickers simultaneously."""
    import ledger_core

    ledger_core.get_all_transactions._cache.clear()

    transactions = [
        {"date": "2025-01-03", "entries": [
            {"ticker": "AAA", "amount": -5.0},
            {"ticker": "USD", "amount": 500.0},
        ]},
        {"date": "2025-01-03", "entries": [
            {"ticker": "BBB", "amount": -3.0},
            {"ticker": "USD", "amount": 300.0},
        ]},
    ]

    fixes = ledger_core.auto_fix_negative_positions(transactions)

    assert len(fixes) == 2
    fix_map = {f[0]: f for f in fixes}
    assert fix_map["AAA"][1] == 5.0
    assert fix_map["BBB"][1] == 3.0


def test_auto_fix_negative_balance_after_fix(tmp: Path):
    """Balance is correct after auto_fix_negative_positions appends fixes."""
    import ledger_core
    import storage

    ledger_core.get_all_transactions._cache.clear()

    transactions = [
        {"date": "2025-01-03", "entries": [
            {"ticker": "S2B.WA", "amount": -5.7315},
            {"ticker": "PLN", "amount": 193.50},
        ]},
    ]

    fixes = ledger_core.auto_fix_negative_positions(transactions)
    assert len(fixes) == 1

    # Simulate what import_xtb does: add all transactions to ledger
    for rec in transactions:
        ledger_core.add_transaction(rec["date"], rec["entries"])

    bal = storage.load_balance()
    # Sell (-5.7315) + auto-fix (+5.7315) = 0 → ticker removed from balance
    assert "S2B.WA" not in bal or abs(bal["S2B.WA"]["amount"]) < 1e-6
    # PLN received the cash
    assert bal["PLN"]["amount"] == 193.50


def test_auto_fix_negative_reimport_idempotent(tmp: Path):
    """Re-importing after a negative-position fix is deduped at the entry level.

    auto_fix_negative_positions itself re-detects the negative on re-import
    (it has no ledger-level dedup like auto_fix_splits). The actual prevention
    of duplicate entries happens in import_xtb via _existing_entry_counts().
    """
    import ledger_core
    import storage

    ledger_core.get_all_transactions._cache.clear()

    transactions = [
        {"date": "2025-05-20", "entries": [
            {"ticker": "S2B.WA", "amount": -5.7315},
            {"ticker": "PLN", "amount": 193.50},
        ]},
    ]

    # First call: fixes the negative
    fixes1 = ledger_core.auto_fix_negative_positions(transactions)
    assert len(fixes1) == 1

    # Add all transactions (sell + fix) to the ledger
    for rec in transactions:
        ledger_core.add_transaction(rec["date"], rec["entries"])

    # Second import: starting_balance now includes the fix (+5.7315 - 5.7315 = 0)
    # The new sell (-5.7315) makes it negative again → function re-detects
    transactions2 = [
        {"date": "2025-05-20", "entries": [
            {"ticker": "S2B.WA", "amount": -5.7315},
            {"ticker": "PLN", "amount": 193.50},
        ]},
    ]

    from ledger_core import get_all_transactions
    starting = {}
    for rec in get_all_transactions():
        for e in rec["entries"]:
            t = e["ticker"].upper()
            if t not in storage.SUPPORTED_CURRENCIES:
                starting[t] = starting.get(t, 0.0) + float(e["amount"])

    # Function re-detects the negative (expected — dedup is at entry level)
    fixes2 = ledger_core.auto_fix_negative_positions(
        transactions2, starting_balance=starting
    )
    assert len(fixes2) == 1

    # But at the ledger level, _existing_entry_counts dedup prevents duplicates.
    # Simulate: build the dedup map from current ledger
    from ledger_core import existing_entry_counts
    counts = existing_entry_counts()

    # The fix entry (date, ticker, amount) should already be consumed
    fix_key = ("2025-05-20", "S2B.WA", round(5.7315, 8))
    assert counts.get(fix_key, 0) == 1, "Fix entry exists exactly once in ledger"


def test_auto_fix_negative_starting_balance_triggers_fix(tmp: Path):
    """auto_fix_negative_positions fixes when starting_balance + sell goes negative."""
    import ledger_core

    ledger_core.get_all_transactions._cache.clear()

    # User has 10 shares, sells 15 → goes negative by 5
    transactions = [
        {"date": "2025-01-10", "entries": [
            {"ticker": "XYZ", "amount": -15.0},
            {"ticker": "USD", "amount": 1500.0},
        ]},
    ]

    starting = {"XYZ": 10.0}
    fixes = ledger_core.auto_fix_negative_positions(
        transactions, starting_balance=starting
    )

    assert len(fixes) == 1
    assert fixes[0][0] == "XYZ"
    assert abs(fixes[0][1] - 5.0) < 1e-6  # missing 5 shares


def test_auto_fix_negative_no_fix_when_starting_covers(tmp: Path):
    """auto_fix_negative_positions does NOT fix when starting_balance covers the sell."""
    import ledger_core

    ledger_core.get_all_transactions._cache.clear()

    transactions = [
        {"date": "2025-01-10", "entries": [
            {"ticker": "XYZ", "amount": -10.0},
            {"ticker": "USD", "amount": 1000.0},
        ]},
    ]

    starting = {"XYZ": 15.0}  # enough to cover the sell
    fixes = ledger_core.auto_fix_negative_positions(
        transactions, starting_balance=starting
    )

    assert fixes == []


def test_auto_fix_negative_empty_transactions(tmp: Path):
    """auto_fix_negative_positions returns empty list for empty transactions."""
    import ledger_core

    fixes = ledger_core.auto_fix_negative_positions([])
    assert fixes == []


def test_auto_fix_negative_first_negative_date(tmp: Path):
    """auto_fix_negative_positions inserts fix on the correct date (first negative)."""
    import ledger_core

    ledger_core.get_all_transactions._cache.clear()

    transactions = [
        {"date": "2025-01-03", "entries": [
            {"ticker": "ABC", "amount": 5.0},
            {"ticker": "USD", "amount": -500.0},
        ]},
        {"date": "2025-02-01", "entries": [
            {"ticker": "ABC", "amount": -8.0},
            {"ticker": "USD", "amount": 800.0},
        ]},
    ]

    fixes = ledger_core.auto_fix_negative_positions(transactions)

    assert len(fixes) == 1
    assert fixes[0][0] == "ABC"
    assert fixes[0][2] == "2025-02-01"  # first date position goes negative


def test_auto_fix_negative_and_split_on_same_ticker(tmp: Path, monkeypatch):
    """Both auto-fixes can fire on the same ticker in one import session."""
    import ledger_core
    import ticker_data
    import storage

    ledger_core.get_all_transactions._cache.clear()

    # Simulate a sell from a corporate action (no prior buy → negative)
    # and also an unapplied split on the same ticker
    transactions = [
        {"date": "2025-01-03", "entries": [
            {"ticker": "SYN", "amount": 3.0},
            {"ticker": "USD", "amount": -300.0},
        ]},
        {"date": "2025-06-01", "entries": [
            {"ticker": "SYN", "amount": -6.0},
            {"ticker": "USD", "amount": 600.0},
        ]},
    ]

    # SYN had a 2:1 split in March
    monkeypatch.setattr(ticker_data, "get_splits", lambda t: {
        "2025-03-01": 2.0,
    } if t == "SYN" else {})

    # First: fix the negative position
    neg_fixes = ledger_core.auto_fix_negative_positions(transactions)
    assert len(neg_fixes) == 1  # -3 shares → fix with +3

    # Then: fix the split (balance_before=3+3=6, expected=12, but we sold 6
    # on 2025-06-01 which is after the split, so balance_after=6, missing=6)
    split_fixes = ledger_core.auto_fix_splits(transactions)
    assert len(split_fixes) == 1
    assert split_fixes[0][0] == "SYN"
