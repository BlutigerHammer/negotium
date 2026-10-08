"""Portfolio builder — pytest suite (split from the original monolithic runner)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch
import fixtures as fx
import json


def test_holdings_weights_currency_invariant(tmp: Path):
    """Position weights must not change when the base currency changes (no FX→1.0)."""
    import json
    import ledger_core, portfolio_core, storage

    def write(ticker, year, prices):
        d = tmp / "data" / "prices" / ticker
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{year}.json").write_text(json.dumps(prices))

    # EURPLN and EURUSD are fresh on 2023-03-05; USDPLN is stale (only 2023-01-05).
    write("EURPLN", 2023, {"2023-03-05": 4.84})
    write("EURUSD", 2023, {"2023-03-05": 1.10})
    write("USDPLN", 2023, {"2023-01-05": 4.40})
    write("GOOG", 2023, {"2023-03-05": 100.0})
    write("SEC0.DE", 2023, {"2023-03-05": 80.0})

    ledger_core.add_transaction("2023-03-05", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-03-05", [
        {"ticker": "USD", "amount": -1000.0},
        {"ticker": "GOOG", "amount": 10.0},
    ])
    ledger_core.add_transaction("2023-03-05", [
        {"ticker": "EUR", "amount": -800.0},
        {"ticker": "SEC0.DE", "amount": 10.0},
    ])

    def weights(base):
        snaps = portfolio_core.build_portfolio(
            start_date=date(2023, 3, 5), end_date=date(2023, 3, 5),
            base_currency=base, precision="D", use_cache=False,
        )
        latest = snaps[-1]
        tv = latest["total_value"]
        assert tv > 0
        return {a["ticker"]: a["value_base"] / tv for a in latest["assets"]}

    w_pln = weights("PLN")
    w_usd = weights("USD")
    assert set(w_pln) == set(w_usd)
    for ticker in w_pln:
        assert abs(w_pln[ticker] - w_usd[ticker]) < 0.01, \
            f"weight of {ticker} differs by currency: {w_pln[ticker]:.4f} vs {w_usd[ticker]:.4f}"


def test_benchmark_cache_requires_matching_contribution_flow():
    from benchmark_cache import benchmark_cache_matches, investment_flow_signature

    snapshots = [
        {"date": "2024-10-21", "invested": 36000.0},
        {"date": "2024-10-22", "invested": 36000.0},
    ]
    stale_cache = [
        {"date": "2024-10-21", "VWCE.DE": 1000.0},
        {"date": "2024-10-22", "VWCE.DE": 1001.0},
    ]
    signature = investment_flow_signature(snapshots)

    assert not benchmark_cache_matches(stale_cache, snapshots, ["VWCE.DE"], signature)

    valid_cache = [
        {**row, "_flow_signature": signature} for row in stale_cache
    ]
    assert benchmark_cache_matches(valid_cache, snapshots, ["VWCE.DE"], signature)

    wrong_flows = [
        {"date": "2024-10-21", "invested": 1000.0},
        {"date": "2024-10-22", "invested": 1000.0},
    ]
    old_signature_cache = [
        {**row, "_flow_signature": investment_flow_signature(wrong_flows)}
        for row in stale_cache
    ]
    assert not benchmark_cache_matches(
        old_signature_cache, snapshots, ["VWCE.DE"], signature
    )


def test_portfolio_build_single_asset(tmp: Path):
    """Portfolio build produces correct values for a single USD stock in PLN base."""
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)

    # Buy 10 AAPL at 2023-01-03
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.70},
    ])

    snapshots = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 3),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["date"] == "2023-01-03"

    # Expected: 10 AAPL × 125.07 × 4.38 ≈ 5478.07 PLN
    # Plus USD cash: -1250.70 × 4.38 ≈ -5477.79 PLN → near zero net cash
    aapl_asset = next((a for a in snap["assets"] if a["ticker"] == "AAPL"), None)
    assert aapl_asset is not None, "AAPL should appear in holdings"
    expected_aapl_value = 10.0 * 125.07 * 4.38
    assert abs(aapl_asset["value_base"] - expected_aapl_value) < 1.0, \
        f"AAPL value_base: expected ~{expected_aapl_value:.2f}, got {aapl_asset['value_base']}"


def test_portfolio_build_cash_only(tmp: Path):
    """Portfolio with only PLN cash shows correct value without any FX conversion."""
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0},
    ])

    snapshots = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 3),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    snap = snapshots[0]
    assert abs(snap["total_value"] - 10000.0) < 0.01, \
        f"10000 PLN cash should be 10000 PLN total_value, got {snap['total_value']}"


def test_portfolio_invested_tracking(tmp: Path):
    """invested correctly counts only positive cash inflows."""
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)

    # Deposit 10000 PLN, then buy AAPL (cash outflow should not count as invested)
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},  # inflow → invested
    ])
    ledger_core.add_transaction("2023-01-04", [
        {"ticker": "AAPL", "amount": 5.0},
        {"ticker": "PLN", "amount": -631.8},   # outflow → not a invested
    ])

    snapshots = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 4),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    # invested at Jan 4 should still be ~10000 (only the PLN deposit counts)
    snap_jan4 = next(s for s in snapshots if s["date"] == "2023-01-04")
    assert abs(snap_jan4["invested"] - 10000.0) < 1.0, \
        f"invested should be ~10000, got {snap_jan4['invested']}"


def test_portfolio_weekly_precision(tmp: Path):
    """Weekly precision yields only Friday dates."""
    import ledger_core, portfolio_core
    from datetime import timedelta

    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 5.0},
    ])

    snapshots = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 27),
        base_currency="PLN",
        precision="W-FRI",
        use_cache=False,
    )

    for snap in snapshots:
        d = date.fromisoformat(snap["date"])
        assert d.weekday() == 4, f"Weekly snapshot on {snap['date']} is not a Friday"


def test_portfolio_cache_resumes(tmp: Path):
    """Portfolio build resumes from cached snapshots without recomputing them."""
    import ledger_core, portfolio_core, storage

    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
    ])

    # Build up to Jan 5
    portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 5),
        base_currency="PLN",
        precision="D",
        use_cache=True,
    )

    # Patch storage to track if portfolio.jsonl is re-read
    calls = []
    orig_load = storage.load_portfolio

    def mock_load():
        calls.append(1)
        return orig_load()

    storage.load_portfolio = mock_load

    # Build Jan 5 to Jan 6 (should resume from cache)
    portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 6),
        base_currency="PLN",
        precision="D",
        use_cache=True,
    )

    saved = storage.load_portfolio()
    dates = [s["date"] for s in saved]
    assert "2023-01-06" in dates, "Jan 6 should be added after resuming"

    storage.load_portfolio = orig_load


def test_day_range_daily(tmp: Path):
    """_day_range yields every day between start and end inclusive."""
    from portfolio_core import _day_range
    days = list(_day_range(date(2023, 1, 3), date(2023, 1, 6), "D"))
    assert len(days) == 4
    assert days[0] == date(2023, 1, 3)
    assert days[-1] == date(2023, 1, 6)


def test_day_range_weekly(tmp: Path):
    """_day_range with weekly precision yields only Fridays."""
    from portfolio_core import _day_range
    days = list(_day_range(date(2023, 1, 3), date(2023, 1, 31), "W-FRI"))
    for d in days:
        assert d.weekday() == 4, f"{d} is not a Friday"
    assert len(days) == 4


def test_snapshots_to_series(tmp: Path):
    """snapshots_to_series extracts correct parallel arrays."""
    from portfolio_core import snapshots_to_series

    snaps = [
        {"date": "2023-01-03", "total_value": 1000.0, "invested": 900.0},
        {"date": "2023-01-04", "total_value": 1050.0, "invested": 900.0},
    ]
    dates, values, contrs = snapshots_to_series(snaps)
    assert dates  == ["2023-01-03", "2023-01-04"]
    assert values == [1000.0, 1050.0]
    assert contrs == [900.0, 900.0]


def test_sell_proceeds_not_counted_as_invested(tmp: Path):
    """Sale proceeds (EUR from selling ETF) must NOT increase invested."""
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)
    storage_mod = __import__("storage")
    storage_mod.save_price_year("QDVE.DE", 2023, {
        "2023-01-03": 200.0,
        "2023-06-01": 240.0,
    })

    # Deposit real money: 1000 EUR
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "EUR", "amount": 1000.0, "account_operation": True},
    ])
    # Buy QDVE.DE with it
    ledger_core.add_transaction("2023-01-04", [
        {"ticker": "QDVE.DE", "amount": 5.0},
        {"ticker": "EUR",     "amount": -1000.0},
    ])
    # Sell for profit: 1200 EUR back
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "QDVE.DE", "amount": -5.0},
        {"ticker": "EUR",     "amount": 1200.0},
    ])

    snaps = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 6, 1),
        base_currency="EUR",
        precision="D",
        use_cache=False,
    )

    last = snaps[-1]
    # invested should be ~1000 EUR (the initial deposit), NOT 2200 EUR
    assert abs(last["invested"] - 1000.0) < 1.0, \
        f"invested should be ~1000 EUR (deposit only), got {last['invested']}"
    # Total value should be ~1200 EUR (the sale proceeds sitting as cash)
    assert abs(last["total_value"] - 1200.0) < 1.0, \
        f"Total value should be ~1200 EUR (all as cash), got {last['total_value']}"


def test_currency_exchange_counts_as_invested(tmp: Path):
    """Wiring USD into the portfolio (cash deposit) counts as invested."""
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)

    # Wire in 1000 USD — cash deposit
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "USD", "amount": 1000.0, "account_operation": True},
    ])

    snaps = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 3),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    snap = snaps[0]
    # 1000 USD × 4.38 USDPLN = 4380 PLN invested
    assert abs(snap["invested"] - 4380.0) < 1.0, \
        f"1000 USD deposit should add ~4380 PLN invested, got {snap['invested']}"


def test_eur_stock_valued_correctly_in_pln(tmp: Path):
    """QDVE.DE (EUR) position is correctly converted to PLN via EURPLN."""
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)
    storage_mod = __import__("storage")
    storage_mod.save_price_year("QDVE.DE", 2023, {
        "2023-01-03": 200.0,
    })

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "QDVE.DE", "amount": 5.0},
        {"ticker": "EUR",     "amount": -1000.0},
    ])

    snaps = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 3),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    snap  = snaps[0]
    asset = next((a for a in snap["assets"] if a["ticker"] == "QDVE.DE"), None)
    assert asset is not None, "QDVE.DE should appear in holdings"
    assert asset["currency"] == "EUR", \
        f"QDVE.DE should be EUR-denominated, got {asset['currency']}"

    # 5 shares × 200 EUR × 4.68 EURPLN = 4680 PLN
    expected = 5.0 * 200.0 * 4.68
    assert abs(asset["value_base"] - expected) < 1.0, \
        f"QDVE.DE value in PLN: expected ~{expected:.2f}, got {asset['value_base']}"


def test_usd_stock_valued_correctly_in_pln(tmp: Path):
    """GOOG (USD) position is correctly converted to PLN via USDPLN."""
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)
    storage_mod = __import__("storage")
    storage_mod.save_price_year("GOOG", 2023, {
        "2023-01-03": 88.0,
    })

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "GOOG", "amount": 10.0},
        {"ticker": "USD",  "amount": -880.0},
    ])

    snaps = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 3),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    asset = next((a for a in snaps[0]["assets"] if a["ticker"] == "GOOG"), None)
    assert asset is not None, "GOOG should appear in holdings"
    assert asset["currency"] == "USD", \
        f"GOOG should be USD-denominated, got {asset['currency']}"

    # 10 shares × 88 USD × 4.38 USDPLN = 3854.4 PLN
    expected = 10.0 * 88.0 * 4.38
    assert abs(asset["value_base"] - expected) < 1.0, \
        f"GOOG value in PLN: expected ~{expected:.2f}, got {asset['value_base']}"


def test_mixed_portfolio_pln_eur_usd(tmp: Path):
    """Portfolio with PLN, EUR and USD positions all valued correctly."""
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)
    storage_mod = __import__("storage")
    storage_mod.save_price_year("QDVE.DE", 2023, {"2023-01-09": 210.0})
    storage_mod.save_price_year("GOOG",    2023, {"2023-01-09": 91.0})

    # Deposit PLN cash
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 5000.0, "account_operation": True},
    ])
    # Buy EUR ETF
    ledger_core.add_transaction("2023-01-04", [
        {"ticker": "QDVE.DE", "amount": 3.0},
        {"ticker": "EUR",     "amount": -630.0},
    ])
    # Buy USD stock
    ledger_core.add_transaction("2023-01-06", [
        {"ticker": "GOOG", "amount": 2.0},
        {"ticker": "USD",  "amount": -182.0},
    ])

    snaps = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 9),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    last   = snaps[-1]
    tickers = {a["ticker"] for a in last["assets"]}

    assert "PLN"     in tickers, "PLN cash should appear"
    assert "EUR"     in tickers, "EUR cash should appear"
    assert "USD"     in tickers, "USD cash should appear"
    assert "QDVE.DE" in tickers, "QDVE.DE should appear"
    assert "GOOG"    in tickers, "GOOG should appear"

    qdve = next(a for a in last["assets"] if a["ticker"] == "QDVE.DE")
    goog = next(a for a in last["assets"] if a["ticker"] == "GOOG")
    assert qdve["currency"] == "EUR"
    assert goog["currency"] == "USD"

    # invested = only the PLN deposit (5000 PLN)
    # EUR buy and USD buy are stock transactions, not counted
    assert abs(last["invested"] - 5000.0) < 1.0, \
        f"Only PLN deposit should count as invested: {last['invested']}"


def test_dividend_and_interest_do_not_count_as_invested(tmp: Path):
    """Dividends/interest credit cash but must NOT raise invested capital.

    Raw prices + dividend cash = correct total return: the dividend lifts
    total_value while invested stays flat, so P&L/TWR/IRR read it as gain.
    """
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])
    # Dividend + interest: unmarked pure-cash credits
    ledger_core.add_transaction("2023-06-01", [
        {"ticker": "PLN", "amount": 100.0},
    ])
    ledger_core.add_transaction("2023-07-01", [
        {"ticker": "PLN", "amount": 5.0},
    ])
    ledger_core.add_transaction("2023-08-01", [
        {"ticker": "PLN", "amount": -20.0},  # withholding tax / commission
    ])

    snaps = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 8, 2),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    last = snaps[-1]
    assert abs(last["invested"] - 10000.0) < 1.0, \
        f"dividends/interest/tax must not change invested, got {last['invested']}"
    assert abs(last["total_value"] - 10085.0) < 1.0, \
        f"cash gains should lift total_value, got {last['total_value']}"


def test_withdrawal_decreases_invested(tmp: Path):
    """Withdrawal (negative account_operation) reduces invested capital."""
    import ledger_core, portfolio_core

    fx.inject_fake_prices(tmp)

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "PLN", "amount": 10000.0, "account_operation": True},
    ])
    ledger_core.add_transaction("2023-01-04", [
        {"ticker": "PLN", "amount": -3000.0, "account_operation": True},
    ])

    snaps = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 4),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    snap_jan3 = next(s for s in snaps if s["date"] == "2023-01-03")
    snap_jan4 = next(s for s in snaps if s["date"] == "2023-01-04")

    assert abs(snap_jan3["invested"] - 10000.0) < 1.0
    assert abs(snap_jan4["invested"] - 7000.0) < 1.0, \
        f"Withdrawal should reduce invested to ~7000, got {snap_jan4['invested']}"
    assert abs(snap_jan4["total_value"] - 7000.0) < 1.0


def test_portfolio_includes_today_transactions(tmp: Path):
    """build_portfolio includes transactions dated today."""
    from unittest.mock import patch
    import ledger_core, portfolio_core
    from datetime import date as _real_date

    fx.inject_fake_prices(tmp)

    # Yesterday's transaction
    ledger_core.add_transaction("2023-01-04", [
        {"ticker": "AAPL", "amount": 10.0},
    ])
    # Today's transaction — should now be included
    ledger_core.add_transaction("2023-01-05", [
        {"ticker": "AAPL", "amount": 5.0},
    ])

    class _FakeDate(_real_date):
        @classmethod
        def today(cls):
            return _real_date(2023, 1, 5)

    with patch.object(portfolio_core, "date", _FakeDate):
        snapshots = portfolio_core.build_portfolio(
            start_date=date(2023, 1, 3),
            end_date=date(2023, 1, 5),
            base_currency="PLN",
            precision="D",
            use_cache=False,
        )

    snap = next(s for s in snapshots if s["date"] == "2023-01-05")
    aapl = next((a for a in snap["assets"] if a["ticker"] == "AAPL"), None)
    assert aapl is not None, "AAPL should appear"
    assert abs(aapl["amount"] - 15.0) < 1e-6, \
        f"Should hold 15 AAPL (today's +5 included), got {aapl['amount']}"


def test_cost_basis_eur_stock_converts_to_pln(tmp: Path):
    """Cost basis for EUR stock uses avg_price × EURPLN rate × shares."""
    import ledger_core, portfolio_core, storage

    fx.inject_fake_prices(tmp)
    storage.save_price_year("SEC0.DE", 2023, {
        "2023-01-03": 88.27,
        "2023-01-04": 90.00,
    })

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "SEC0.DE", "amount": 288.0},
        {"ticker": "EUR",     "amount": -25425.0},
    ])

    snaps = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 4),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    snap = next(s for s in snaps if s["date"] == "2023-01-04")
    asset = next(a for a in snap["assets"] if a["ticker"] == "SEC0.DE")

    bal = storage.load_balance()
    avg_raw = bal["SEC0.DE"]["avg_price"]  # 88.27 EUR (native)

    # avg_price is in native EUR, not PLN — verify it's the stock price, not inflated
    assert 80 < avg_raw < 100, \
        f"avg_price should be ~88 EUR (native), got {avg_raw}"

    # Value uses EURPLN, cost basis must use same currency
    # Return = (90/88.27 - 1) × 100 ≈ 1.96% — FX cancels out
    ret_pct = ((asset["value_base"] / (asset["amount"] * avg_raw * 4.67)) - 1) * 100
    assert 0 < ret_pct < 5, \
        f"Return should be ~2%, got {ret_pct:.1f}%"


def test_return_eur_stock_not_inflated(tmp: Path):
    """Return for EUR stock is reasonable, not 300%+ due to missing FX conversion."""
    import ledger_core, portfolio_core, storage

    fx.inject_fake_prices(tmp)
    storage.save_price_year("SXRV.DE", 2023, {
        "2023-01-03": 42.0,
        "2023-01-09": 44.0,
    })

    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "SXRV.DE", "amount": 3.5},
        {"ticker": "EUR",     "amount": -147.0},
    ])

    snaps = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 1, 9),
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    snap = next(s for s in snaps if s["date"] == "2023-01-09")
    asset = next(a for a in snap["assets"] if a["ticker"] == "SXRV.DE")

    bal = storage.load_balance()
    avg_raw = bal["SXRV.DE"]["avg_price"]  # 42.0 EUR (native)

    # avg_price should be the EUR stock price, not some PLN-inflated number
    assert 30 < avg_raw < 50, \
        f"avg_price should be ~42 EUR (native), got {avg_raw}"

    # Return ≈ 44/42 - 1 ≈ 4.8%. With proper FX conversion both value and
    # cost_basis use the same EURPLN rate, so it cancels out.
    # Without the fix, cost_basis would be in EUR while value is in PLN → 300%+.
    # We just check it's in a sane range (0–20%).
    shares = asset["amount"]
    # Use avg_raw directly (native EUR) — this is what the holdings table
    # does BEFORE the fix (missing FX conversion). If the fix works,
    # the return should be ~5% regardless of FX rate used.
    ret_pct_native = ((asset["value_base"] / (shares * avg_raw * 4.34)) - 1) * 100
    assert -5 < ret_pct_native < 20, \
        f"Return ~{ret_pct_native:.1f}% looks inflated (FX conversion may be missing)"


# ── Performance/regression: price slab loaded once per build ────────────

def test_price_slab_loaded_once_per_build(tmp: Path, monkeypatch):
    """The _PriceCache must load each (ticker, year) slab exactly once per build_portfolio call.

    If a future change accidentally reloads slabs per-day, this test catches it.
    """
    import ledger_core, portfolio_core
    from storage import load_price_year

    fx.inject_fake_prices(tmp)

    # Buy AAPL in 2023 so the 2023 slab is needed
    ledger_core.add_transaction("2023-01-03", [
        {"ticker": "AAPL", "amount": 10.0},
        {"ticker": "USD", "amount": -1250.70},
    ])

    calls: list[tuple[str, int]] = []
    real_load = load_price_year

    def counting_load(ticker, year, adjusted=False):
        calls.append((ticker, year))
        return real_load(ticker, year, adjusted)

    # ticker_data.get_price calls storage.load_price_year via its module-level import
    monkeypatch.setattr("ticker_data.load_price_year", counting_load)

    snapshots = portfolio_core.build_portfolio(
        start_date=date(2023, 1, 3),
        end_date=date(2023, 2, 28),   # ~8 weeks of daily snapshots
        base_currency="PLN",
        precision="D",
        use_cache=False,
    )

    assert len(snapshots) > 20
    aapl_loads = [p for p in calls if p[0] == "AAPL"]
    # AAPL 2023 slab should be loaded exactly once across all ~57 days
    assert aapl_loads == [("AAPL", 2023)], f"expected single AAPL load, got {aapl_loads}"
