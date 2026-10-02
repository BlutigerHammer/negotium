from datetime import date

from bonds import BondDefinition, bond_value, is_retail_bond, retail_bond_metadata


def test_edo_ticker_detection():
    assert is_retail_bond("EDO0936")
    assert not is_retail_bond("AAPL")
    assert not is_retail_bond("EDO")


def test_retail_bond_metadata_maps_to_polish_fixed_income():
    assert retail_bond_metadata("EDO0936") == {
        "sector": "Government bonds",
        "country": "Poland",
        "asset_class": "Fixed income",
    }
    assert retail_bond_metadata("AAPL") is None


def test_edo_compounds_first_rate_then_inflation_plus_margin():
    definition = BondDefinition(
        ticker="EDO0526",
        issue_date=date(2026, 5, 1),
        maturity_years=10,
        first_year_rate=0.05,
        margin=0.015,
    )
    value = bond_value(
        definition,
        units=10,
        purchase_date=date(2026, 5, 20),
        as_of=date(2028, 5, 20),
        inflation={2027: 0.04},
    )
    assert value == 1107.80


def test_edo_before_first_anniversary_is_nominal():
    definition = BondDefinition("EDO0526", date(2026, 5, 1), 10, 0.05, 0.015)
    assert bond_value(definition, 3, date(2026, 5, 20), date(2027, 5, 19), {}) == 314.97


def test_edo_leap_day_purchase_values_after_first_anniversary():
    definition = BondDefinition("EDO0224", date(2024, 2, 1), 10, 0.05, 0.015)
    value = bond_value(
        definition,
        units=1,
        purchase_date=date(2024, 2, 29),
        as_of=date(2026, 3, 1),
        inflation={"2024-12": 0.03, "2025-12": 0.03},
    )
    assert value > 100.0


def test_tos_uses_fixed_rate_for_each_year():
    definition = BondDefinition("TOS0827", date(2024, 8, 30), 3, 0.062, 0.0, False)
    value = bond_value(definition, 50, date(2024, 8, 30), date(2026, 8, 30), {2025: 0.036})
    assert value == 5639.00


def test_edo_uses_cpi_from_two_months_before_period_start():
    definition = BondDefinition("EDO0526", date(2026, 5, 1), 10, 0.05, 0.015)
    value = bond_value(
        definition,
        units=1,
        purchase_date=date(2026, 5, 20),
        as_of=date(2028, 5, 20),
        inflation={"2027-03": 0.04},
    )
    assert value == 110.78


def test_tos_includes_current_period_accrual():
    definition = BondDefinition("TOS0827", date(2024, 8, 30), 3, 0.062, 0.0, False)
    value = bond_value(definition, 50, date(2024, 8, 30), date(2026, 9, 20), {})
    assert value == 5659.50


