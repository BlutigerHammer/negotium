"""Polish retail Treasury bond valuation helpers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent.parent
BONDS_PATH = ROOT / "data" / "bonds.json"
INFLATION_PATH = ROOT / "data" / "inflation.json"
CPI_MONTHLY_PATH = ROOT / "data" / "inflation_monthly.json"

_BOND_TYPES = {
    "EDO": (10, "obligacje-10-letnie-edo"),
    "TOS": (3, "obligacje-3-letnie-tos"),
}


@dataclass(frozen=True)
class BondDefinition:
    ticker: str
    issue_date: date
    maturity_years: int
    first_year_rate: float
    margin: float
    indexed: bool = True


def is_retail_bond(ticker: str) -> bool:
    value = ticker.upper().strip()
    return bool(re.fullmatch(r"(?:EDO|TOS)\d{4}", value))


def retail_bond_metadata(ticker: str) -> dict[str, str] | None:
    if not is_retail_bond(ticker):
        return None
    return {
        "sector": "Government bonds",
        "country": "Poland",
        "asset_class": "Fixed income",
    }


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _issue_date(ticker: str, maturity_years: int) -> date:
    maturity_year = 2000 + int(ticker[5:])
    return date(maturity_year - maturity_years, int(ticker[3:5]), 1)


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Negotium/1.0"})
    with urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")


def _parse_percent(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return float(match.group(1).replace(",", ".")) / 100


def load_bond_definition(ticker: str) -> BondDefinition | None:
    """Load cached issue data, or discover it from the official offer page."""
    ticker = ticker.upper().strip()
    if not is_retail_bond(ticker):
        return None
    cached = _load_json(BONDS_PATH).get(ticker)
    if cached:
        maturity_years = int(cached["maturity_years"])
        return BondDefinition(
            ticker=ticker,
            issue_date=_issue_date(ticker, maturity_years),
            maturity_years=maturity_years,
            first_year_rate=float(cached["first_year_rate"]),
            margin=float(cached["margin"]),
            indexed=ticker.startswith("EDO"),
        )

    kind = ticker[:3]
    maturity_years, slug = _BOND_TYPES[kind]
    url = f"https://www.obligacjeskarbowe.pl/oferta-obligacji/{slug}/{ticker.lower()}/"
    try:
        html = _fetch_text(url)
        first_rate = _parse_percent(
            html,
            r"(?:oprocentowanie|oprocentowaniu).*?(\d{1,2}[,.]\d{1,2})\s*%",
        )
        if first_rate is None:
            first_rate = _parse_percent(html, r"(\d{1,2}[,.]\d{1,2})\s*<sub>\s*%")
        margin = _parse_percent(html, r"mar(?:ż|z)a.{0,180}?(\d{1,2}[,.]\d{1,2})\s*%")
    except Exception:
        return None

    if first_rate is None:
        return None
    definition = BondDefinition(
        ticker=ticker,
        issue_date=_issue_date(ticker, maturity_years),
        maturity_years=maturity_years,
        first_year_rate=first_rate,
        margin=margin or 0.0,
        indexed=kind == "EDO",
    )
    cache = _load_json(BONDS_PATH)
    cache[ticker] = {
        "issue_date": definition.issue_date.isoformat(),
        "maturity_years": maturity_years,
        "first_year_rate": definition.first_year_rate,
        "margin": definition.margin,
        "source": url,
    }
    _save_json(BONDS_PATH, cache)
    return definition


def load_inflation() -> dict[int, float]:
    return {int(year): float(value) for year, value in _load_json(INFLATION_PATH).items()}


def save_inflation(values: dict[int, float]) -> None:
    _save_json(INFLATION_PATH, {str(year): value for year, value in values.items()})


def load_monthly_cpi() -> dict[str, float]:
    return {str(month): float(value) for month, value in _load_json(CPI_MONTHLY_PATH).items()}


def _fetch_annual_cpi_year(year: int) -> dict[str, float]:
    url = (
        "https://api-sdp.stat.gov.pl/api/1.1.0/indicators/"
        f"indicator-data-indicator?id-wskaznik=1832&id-rok={year}&lang=pl"
    )
    payload = json.loads(_fetch_text(url))
    result: dict[str, float] = {}
    for row in payload:
        period_id = int(row.get("id-okres", 0))
        value = row.get("wartosc")
        if 247 <= period_id <= 258 and value is not None:
            month = period_id - 246
            result[f"{year:04d}-{month:02d}"] = float(value) / 100.0 - 1.0
    return result


def _fetch_variable_cpi_year(year: int) -> dict[str, float]:
    """Fetch current CPI from the post-2025 GUS variable endpoint."""
    result: dict[str, float] = {}
    for period_id in range(247, 259):
        for page in range(1, 50):
            url = (
                "https://api-sdp.stat.gov.pl/api/1.1.0/variable/variable-data-section"
                f"?id-zmienna=305&id-przekroj=1698&id-rok={year}"
                f"&id-okres={period_id}&page-size=5000&page={page}&lang=pl"
            )
            try:
                payload = json.loads(_fetch_text(url))
            except (KeyError, TypeError, ValueError, OSError):
                break
            rows = payload.get("data", [])
            match = next(
                (
                    row for row in rows
                    if (
                        row.get("id-pozycja-2") == 14151055
                        and row.get("id-pozycja-3") == 6902025
                    )
                    and row.get("id-sposob-prezentacji-miara") == 5
                    and row.get("wartosc") is not None
                ),
                None,
            )
            if match is not None:
                month = period_id - 246
                result[f"{year:04d}-{month:02d}"] = float(match["wartosc"]) / 100.0 - 1.0
                break
            if len(rows) < 5000:
                break
    return result


def refresh_monthly_cpi_from_gus(start_year: int, end_year: int) -> dict[str, float]:
    """Fetch official year-on-year CPI indexed by calendar month from GUS."""
    values = load_monthly_cpi()
    for year in range(start_year, end_year + 1):
        try:
            if year >= 2026:
                values.update(_fetch_variable_cpi_year(year))
            else:
                values.update(_fetch_annual_cpi_year(year))
        except (KeyError, TypeError, ValueError, OSError):
            continue
    _save_json(CPI_MONTHLY_PATH, values)
    return values


def refresh_inflation_from_gus() -> dict[int, float]:
    """Fetch annual Polish CPI from the official GUS BDL API.

    GUS variable 217230 is the annual CPI for Poland, where 103.6 means
    prices were 103.6% of the previous year's level, i.e. 3.6% inflation.
    """
    url = "https://bdl.stat.gov.pl/api/v1/data/by-variable/217230"
    try:
        payload = json.loads(_fetch_text(url))
        values = {
            int(item["year"]): round(float(item["val"]) / 100.0 - 1.0, 6)
            for item in payload["data"][0]["attributes"]["values"]
            if item.get("val") is not None
        }
    except (KeyError, TypeError, ValueError, OSError):
        return load_inflation()
    save_inflation(values)
    return values


def _anniversary_date(purchase_date: date, years: int) -> date:
    try:
        return purchase_date.replace(year=purchase_date.year + years)
    except ValueError:
        return purchase_date.replace(year=purchase_date.year + years, day=28)


def bond_value(definition: BondDefinition, units: float, purchase_date: date, as_of: date,
               inflation: dict) -> float:
    """Return gross value, including interest accrued in the current period."""
    if as_of <= purchase_date:
        return units * 100.0

    value = Decimal("100.00")
    anniversary = purchase_date
    for period in range(definition.maturity_years):
        if not definition.indexed:
            rate = Decimal(str(definition.first_year_rate))
        elif period == 0:
            rate = Decimal(str(definition.first_year_rate))
        else:
            period_start = _anniversary_date(purchase_date, period)
            reference_month = period_start.month - 2
            reference_year = period_start.year
            if reference_month <= 0:
                reference_month += 12
                reference_year -= 1
            cpi_key = f"{reference_year:04d}-{reference_month:02d}"
            cpi = inflation.get(cpi_key)
            if cpi is None:
                cpi = inflation.get(reference_year, 0.0)
            rate = max(Decimal(str(cpi or 0.0)), Decimal("0")) + Decimal(str(definition.margin))

        next_anniversary = _anniversary_date(purchase_date, period + 1)

        if as_of >= next_anniversary:
            value *= Decimal("1") + rate
            anniversary = next_anniversary
            continue

        accrued_days = (as_of - anniversary).days
        period_days = (next_anniversary - anniversary).days
        if accrued_days > 0:
            value *= Decimal("1") + rate * Decimal(accrued_days) / Decimal(period_days)
        break
    # The issuer rounds each bond to grosz before multiplying by the holding
    # count. Rounding the aggregate instead produces visible differences for
    # larger holdings.
    per_unit = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float((per_unit * Decimal(str(units))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def bond_value_from_holding(holding: dict, as_of: date, inflation: dict[int, float]) -> float:
    """Value a holding record whose maturity date is the only date supplied."""
    ticker = str(holding["ticker"]).upper().strip()
    definition = load_bond_definition(ticker)
    if definition is None:
        return 0.0
    purchase_date = bond_purchase_date(holding, definition)
    return bond_value(definition, float(holding["units"]), purchase_date, as_of, inflation)


def bond_purchase_date(holding: dict, definition: BondDefinition | None = None) -> date:
    """Infer the purchase date from the maturity date and issue term."""
    if definition is None:
        definition = load_bond_definition(str(holding["ticker"]))
    if definition is None:
        raise ValueError(f"Unknown retail bond: {holding.get('ticker')}")
    maturity = date.fromisoformat(str(holding["maturity_date"]))
    try:
        return maturity.replace(year=maturity.year - definition.maturity_years)
    except ValueError:
        return maturity.replace(year=maturity.year - definition.maturity_years, day=28)
