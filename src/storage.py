"""
storage.py — low-level file I/O helpers with multi-project support

Layout:
  ROOT/data/prices/{TICKER}/{YEAR}.json  — shared price cache
  ROOT/data/projects.json                — project registry
  ROOT/data/{project}/transactions.jsonl — transaction ledger
  ROOT/data/{project}/portfolio.jsonl    — computed snapshots
  ROOT/data/{project}/balance.json       — current holdings
  ROOT/data/{project}/benchmarks_{CCY}.json — benchmark values
  ROOT/data/{project}/imports/           — per-project import files
"""
from __future__ import annotations

try:
    import orjson
    def _loads(data: bytes):
        return orjson.loads(data)
    def _dumps(obj) -> str:
        return orjson.dumps(obj).decode()
except ImportError:
    import json
    _loads = json.loads
    _dumps = lambda obj: json.dumps(obj, ensure_ascii=False)
import os
from pathlib import Path
from datetime import date, datetime
from typing import Iterator
import threading

ROOT = Path(__file__).parent.parent

DATA_ROOT     = ROOT / "data"
PRICES_DIR    = DATA_ROOT / "prices"
PROJECTS_PATH = DATA_ROOT / "projects.json"

# ── Current project state ─────────────────────────────────────────────────────
# The active project resolves per Streamlit session first (each browser
# session has its own st.session_state), falling back to a process-wide value
# for tests and non-UI scripts. Deliberately NO module-level path constants:
# with them, two sessions rerunning concurrently could read and write each
# other's project files. Paths are derived from the project on every call.

_SESSION_PROJECT_KEY = "negotium_current_project"
_current_project: str | None = None


def current_project() -> str | None:
    """Return the active project: session-scoped first, then process fallback."""
    try:
        import streamlit as st
        val = st.session_state.get(_SESSION_PROJECT_KEY)
        if val:
            return str(val)
    except Exception:
        pass  # no Streamlit runtime (tests, plain scripts) → process fallback
    return _current_project


def get_current_project() -> str | None:
    """Alias of current_project()."""
    return current_project()


def set_current_project(name: str) -> None:
    """Set the active project for this session (and the process fallback)."""
    global _current_project
    _current_project = name
    try:
        import streamlit as st
        st.session_state[_SESSION_PROJECT_KEY] = name
    except Exception:
        pass


def _project_dir(name: str | None = None) -> Path:
    """Return the data directory for a project."""
    n = name or current_project()
    if n is None:
        raise RuntimeError("No project selected. Call set_current_project() first.")
    return DATA_ROOT / n


def transactions_path() -> Path:
    """Path of the current project's transaction ledger."""
    return _project_dir() / "transactions.jsonl"


def portfolio_path() -> Path:
    """Path of the current project's computed snapshot cache."""
    return _project_dir() / "portfolio.jsonl"


def balance_path() -> Path:
    """Path of the current project's balance.json."""
    return _project_dir() / "balance.json"


def imports_dir() -> Path:
    """Path of the current project's imports directory."""
    return _project_dir() / "imports"


def bond_holdings_path() -> Path:
    """Path of the current project's retail bond holdings."""
    return _project_dir() / "bond_holdings.json"


def load_bond_holdings() -> list[dict]:
    """Return manually entered retail bond holdings for the current project."""
    path = bond_holdings_path()
    if not path.exists():
        return []
    data = _loads(path.read_bytes())
    return data if isinstance(data, list) else []


def save_bond_holdings(holdings: list[dict]) -> None:
    """Persist retail bond holdings for the current project."""
    _write_bytes_atomic(bond_holdings_path(), _dumps(holdings).encode())


def add_bond_units(ticker: str, maturity_date: str, units: float) -> float:
    """Add units to a series without replacing its existing project holding."""
    ticker = ticker.upper().strip()
    holdings = load_bond_holdings()
    matching = [
        holding for holding in holdings
        if str(holding.get("ticker", "")).upper() == ticker
    ]
    if any(holding.get("maturity_date") != maturity_date for holding in matching):
        raise ValueError(f"{ticker} already has a different maturity date")

    total_units = round(
        sum(float(holding["units"]) for holding in matching) + float(units), 8
    )
    holdings = [
        holding for holding in holdings
        if str(holding.get("ticker", "")).upper() != ticker
    ]
    holdings.append({
        "ticker": ticker,
        "maturity_date": maturity_date,
        "units": total_units,
    })
    save_bond_holdings(holdings)
    return total_units


# ── Project registry ──────────────────────────────────────────────────────────

def list_projects() -> list[str]:
    """Return sorted list of project names."""
    if not PROJECTS_PATH.exists():
        return []
    return sorted(_loads(PROJECTS_PATH.read_bytes()).keys())


def _load_registry() -> dict:
    if not PROJECTS_PATH.exists():
        return {}
    return _loads(PROJECTS_PATH.read_bytes())


def _save_registry(reg: dict) -> None:
    _write_bytes_atomic(PROJECTS_PATH, _dumps(reg).encode())


def get_last_refresh(name: str | None = None) -> str:
    """Return the ISO date of the last data refresh for a project ('' if none)."""
    name = name or current_project()
    if name is None:
        return ""
    reg = _load_registry()
    return reg.get(name, {}).get("last_refresh", "")


def set_last_refresh(date_str: str, name: str | None = None) -> None:
    """Persist the last refresh date for a project in the registry."""
    name = name or current_project()
    if name is None:
        return
    reg = _load_registry()
    entry = reg.setdefault(name, {})
    entry["last_refresh"] = date_str
    _save_registry(reg)


def get_watchlist(name: str | None = None) -> list[str]:
    """Return the per-project watchlist tickers (empty list if none)."""
    name = name or current_project()
    if name is None:
        return []
    reg = _load_registry()
    return list(reg.get(name, {}).get("watchlist", []))


def set_watchlist(tickers: list[str], name: str | None = None) -> None:
    """Persist the per-project watchlist tickers in the registry."""
    name = name or current_project()
    if name is None:
        return
    reg = _load_registry()
    entry = reg.setdefault(name, {})
    entry["watchlist"] = list(tickers)
    _save_registry(reg)


def create_project(name: str) -> None:
    """Create a new empty project."""
    reg = _load_registry()
    if name in reg:
        raise ValueError(f"Project '{name}' already exists")
    _project_dir(name).mkdir(parents=True, exist_ok=True)
    (_project_dir(name) / "imports").mkdir(exist_ok=True)
    reg[name] = {"created_at": datetime.now().isoformat()}
    _save_registry(reg)
    set_current_project(name)


def rename_project(old: str, new: str) -> None:
    """Rename a project directory and update registry."""
    reg = _load_registry()
    if old not in reg:
        raise ValueError(f"Project '{old}' not found")
    if new in reg:
        raise ValueError(f"Project '{new}' already exists")
    old_dir = _project_dir(old)
    new_dir = DATA_ROOT / new
    old_dir.rename(new_dir)
    reg[new] = reg.pop(old)
    _save_registry(reg)
    set_current_project(new)


def delete_project(name: str) -> None:
    """Delete a project and all its data."""
    import shutil
    reg = _load_registry()
    if name not in reg:
        return
    d = _project_dir(name)
    if d.exists():
        shutil.rmtree(d)
    del reg[name]
    _save_registry(reg)
    if current_project() == name:
        global _current_project
        _current_project = None
        try:
            import streamlit as st
            st.session_state.pop(_SESSION_PROJECT_KEY, None)
        except Exception:
            pass


def init_legacy_project() -> str | None:
    """If legacy flat files exist at data/ root, migrate them into a 'default' project.
    Returns the project name if migration happened, else None."""
    legacy_tx = DATA_ROOT / "transactions.jsonl"
    if not legacy_tx.exists():
        return None
    name = "default"
    d = _project_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "imports").mkdir(exist_ok=True)
    for fname in ["transactions.jsonl", "portfolio.jsonl", "balance.json"]:
        src = DATA_ROOT / fname
        dst = d / fname
        if src.exists() and not dst.exists():
            src.rename(dst)
    for p in DATA_ROOT.glob("benchmarks_*.json"):
        dst = d / p.name
        if not dst.exists():
            p.rename(dst)
    build_log = DATA_ROOT / "build.log"
    if build_log.exists():
        build_log.unlink()
    reg = _load_registry()
    reg[name] = {"created_at": datetime.now().isoformat(), "migrated_from": "legacy"}
    _save_registry(reg)
    set_current_project(name)
    return name


# ── Supported currencies ──────────────────────────────────────────────────────
# Single source of truth lives in currencies.py; re-exported here so existing
# `storage.SUPPORTED_CURRENCIES` call sites keep working.

from currencies import (  # noqa: E402,F401 — re-exported for compatibility
    SUPPORTED_CURRENCIES,
    CURRENCY_SUFFIXES,
    SUFFIX_CURRENCY,
    TRIANGULATE_VIA_USD,
)


# ── JSONL helpers ──────────────────────────────────────────────────────────────

def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write bytes via temp file + rename so a crash never leaves a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed dicts from a .jsonl file, skipping blank lines."""
    if not path.exists():
        return
    with path.open("rb") as f:
        for line in f:
            if line.strip():
                yield _loads(line)


def read_jsonl(path: Path) -> list[dict]:
    return list(iter_jsonl(path))


def write_jsonl(path: Path, records: list[dict]) -> None:
    """Atomically overwrite file with one JSON object per line."""
    buf = b"".join(_dumps(rec).encode() + b"\n" for rec in records)
    _write_bytes_atomic(path, buf)


def append_jsonl(path: Path, record: dict) -> None:
    """Append a single record to a .jsonl file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as f:
        f.write(_dumps(record).encode())
        f.write(b"\n")


# ── Balance ────────────────────────────────────────────────────────────────────

def load_balance() -> dict[str, dict]:
    """Return {ticker: {"amount": float, "avg_price": float}} dict."""
    if not balance_path().exists():
        return {}
    data = _loads(balance_path().read_bytes())
    result = {}
    for k, v in data.items():
        if isinstance(v, dict):
            result[k] = v
        else:
            result[k] = {"amount": v, "avg_price": 0.0}
    return result


def save_balance(balance: dict[str, dict]) -> None:
    """Persist balance, removing tickers with ~0 holdings."""
    clean = {}
    for k, v in balance.items():
        amt = v.get("amount", 0.0) if isinstance(v, dict) else v
        if abs(amt) > 1e-9:
            if isinstance(v, dict):
                clean[k] = {"amount": round(amt, 8), "avg_price": round(v.get("avg_price", 0.0), 6)}
            else:
                clean[k] = {"amount": round(amt, 8), "avg_price": 0.0}
    balance_path().parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(balance_path(), _dumps(clean).encode())


# ── Price cache (shared) ──────────────────────────────────────────────────────
# Two namespaces:
#   data/prices/      RAW (unadjusted) closes — the default for everything:
#                     portfolio valuation, charts, TWR/IRR, holdings P&L.
#                     Raw prices keep history immutable across dividend
#                     payouts and match broker cash flows exactly.
#   data/prices_adj/  Dividend-ADJUSTED closes (Yahoo total-return series) —
#                     used only by benchmark what-if overlays so the
#                     comparison includes dividends, like the portfolio does
#                     (dividends land as cash in the ledger).

PRICES_DIR      = DATA_ROOT / "prices"
ADJ_PRICES_DIR  = DATA_ROOT / "prices_adj"


def price_cache_path(ticker: str, year: int, adjusted: bool = False) -> Path:
    base = ADJ_PRICES_DIR if adjusted else PRICES_DIR
    return base / ticker.upper() / f"{year}.json"


def load_price_year(ticker: str, year: int, adjusted: bool = False) -> dict[str, float]:
    """Return {YYYY-MM-DD: close_price} for a ticker/year, or {} if missing."""
    p = price_cache_path(ticker, year, adjusted)
    if not p.exists():
        return {}
    return _loads(p.read_bytes())


def save_price_year(ticker: str, year: int, prices: dict[str, float],
                    adjusted: bool = False) -> None:
    """Persist {YYYY-MM-DD: close_price} for a ticker/year."""
    p = price_cache_path(ticker, year, adjusted)
    _write_bytes_atomic(p, _dumps(prices).encode())


def has_price_year(ticker: str, year: int, adjusted: bool = False) -> bool:
    return price_cache_path(ticker, year, adjusted).exists()


def load_prices_range(ticker: str, start: date, end: date,
                      adjusted: bool = False) -> dict[str, float]:
    """
    Return merged {YYYY-MM-DD: close} for all years in [start.year, end.year].
    Uses the on-disk cache only — call ticker_data.ensure() first.
    """
    result: dict[str, float] = {}
    for year in range(start.year, end.year + 1):
        result.update(load_price_year(ticker, year, adjusted))
    return result


# ── Portfolio snapshots ────────────────────────────────────────────────────────

def load_portfolio() -> list[dict]:
    return read_jsonl(portfolio_path())


def save_portfolio(snapshots: list[dict]) -> None:
    write_jsonl(portfolio_path(), snapshots)


def invalidate_portfolio_from(from_date: str) -> None:
    """
    Remove all portfolio snapshots on or after from_date.
    Called when a transaction is inserted that affects a past date.
    Streams line-by-line to avoid loading the entire file into memory.
    Every line is parsed so the comparison never depends on the JSON
    serializer's exact byte layout (orjson and json format differently).
    """
    if not portfolio_path().exists():
        return
    tmp = portfolio_path().with_suffix(".jsonl.tmp")
    with portfolio_path().open("rb") as src, tmp.open("wb") as dst:
        for line in src:
            stripped = line.strip()
            if not stripped:
                continue
            rec = _loads(stripped)
            if str(rec.get("date", "")) < from_date:
                dst.write(line)
    os.replace(tmp, portfolio_path())


# ── Benchmark cache ──────────────────────────────────────────────────────────

def benchmark_cache_path(base_ccy: str) -> Path:
    return _project_dir() / f"benchmarks_{base_ccy.upper()}.json"


def save_benchmarks(base_ccy: str, data: list[dict]) -> None:
    """Save pre-computed benchmark values. Each entry: {date, ticker: value, ...}."""
    p = benchmark_cache_path(base_ccy)
    _write_bytes_atomic(p, _dumps(data).encode())


def load_benchmarks(base_ccy: str) -> list[dict] | None:
    """Load cached benchmarks, or None if missing."""
    p = benchmark_cache_path(base_ccy)
    if not p.exists():
        return None
    return _loads(p.read_bytes())


# ── Ticker name cache ────────────────────────────────────────────────────────

TICKER_NAMES_PATH = DATA_ROOT / "ticker_names.json"

_ticker_names_cache: dict[str, str] | None = None


def load_ticker_names() -> dict[str, str]:
    """Return {ticker: company_name} from cache, or empty dict."""
    global _ticker_names_cache
    if _ticker_names_cache is not None:
        return _ticker_names_cache
    if not TICKER_NAMES_PATH.exists():
        _ticker_names_cache = {}
        return _ticker_names_cache
    with _cache_lock:
        _ticker_names_cache = _loads(TICKER_NAMES_PATH.read_bytes())
    return _ticker_names_cache


def save_ticker_names(names: dict[str, str]) -> None:
    """Persist {ticker: company_name} cache."""
    global _ticker_names_cache
    _ticker_names_cache = names
    with _cache_lock:
        _write_bytes_atomic(TICKER_NAMES_PATH, _dumps(names).encode())


# ── Ticker metadata cache (sector / country / asset class) ───────────────────

TICKER_META_PATH = DATA_ROOT / "ticker_meta.json"

_ticker_meta_cache: dict | None = None


def load_ticker_meta() -> dict:
    """Return {ticker: {sector, country, asset_class}} from cache, or empty dict."""
    global _ticker_meta_cache
    if _ticker_meta_cache is not None:
        return _ticker_meta_cache
    if not TICKER_META_PATH.exists():
        _ticker_meta_cache = {}
        return _ticker_meta_cache
    with _cache_lock:
        _ticker_meta_cache = _loads(TICKER_META_PATH.read_bytes())
    return _ticker_meta_cache


def save_ticker_meta(meta: dict) -> None:
    """Persist {ticker: {sector, country, asset_class}} cache."""
    global _ticker_meta_cache
    _ticker_meta_cache = meta
    with _cache_lock:
        _write_bytes_atomic(TICKER_META_PATH, _dumps(meta).encode())


# ── ATH cache (per-ticker all-time high, survives restarts) ───────────────────

ATH_PATH = DATA_ROOT / "ath.json"

_ath_disk_cache: dict | None = None
_cache_lock = threading.Lock()


def load_ath() -> dict:
    """Return {ticker: {price, date}} from cache, or empty dict."""
    global _ath_disk_cache
    if _ath_disk_cache is not None:
        return _ath_disk_cache
    if not ATH_PATH.exists():
        _ath_disk_cache = {}
        return _ath_disk_cache
    with _cache_lock:
        _ath_disk_cache = _loads(ATH_PATH.read_bytes())
    return _ath_disk_cache


def save_ath(data: dict) -> None:
    """Persist {ticker: {price, date}} — keeps watchlist ATH offline across restarts."""
    global _ath_disk_cache
    _ath_disk_cache = data
    with _cache_lock:
        _write_bytes_atomic(ATH_PATH, _dumps(data).encode())


# ── Earnings-date cache (per-ticker, survives restarts) ───────────────────────

EARNINGS_PATH = DATA_ROOT / "earnings.json"

_earnings_disk_cache: dict | None = None


def load_earnings() -> dict:
    """Return {ticker: YYYY-MM-DD} from cache, or empty dict."""
    global _earnings_disk_cache
    if _earnings_disk_cache is not None:
        return _earnings_disk_cache
    if not EARNINGS_PATH.exists():
        _earnings_disk_cache = {}
        return _earnings_disk_cache
    with _cache_lock:
        _earnings_disk_cache = _loads(EARNINGS_PATH.read_bytes())
    return _earnings_disk_cache


def save_earnings(data: dict) -> None:
    """Persist {ticker: YYYY-MM-DD} — keeps earnings dates offline across restarts."""
    global _earnings_disk_cache
    _earnings_disk_cache = data
    with _cache_lock:
        _write_bytes_atomic(EARNINGS_PATH, _dumps(data).encode())


# ── Dividend cache (per-ticker ex-date → dividend per share) ──────────────────

DIVIDENDS_PATH = DATA_ROOT / "dividends.json"

_dividends_cache: dict | None = None


def load_dividends() -> dict:
    """Return {ticker: {YYYY-MM-DD: dividend_per_share}} from cache, or empty dict."""
    global _dividends_cache
    if _dividends_cache is not None:
        return _dividends_cache
    if not DIVIDENDS_PATH.exists():
        _dividends_cache = {}
        return _dividends_cache
    with _cache_lock:
        _dividends_cache = _loads(DIVIDENDS_PATH.read_bytes())
    return _dividends_cache


def save_dividends(dividends: dict) -> None:
    """Persist {ticker: {YYYY-MM-DD: dividend_per_share}} cache."""
    global _dividends_cache
    _dividends_cache = dividends
    with _cache_lock:
        _write_bytes_atomic(DIVIDENDS_PATH, _dumps(dividends).encode())


# ── Split cache (per-ticker ex-date → split ratio) ──────────────────────────

SPLITS_PATH = DATA_ROOT / "splits.json"

_splits_cache: dict | None = None


def load_splits() -> dict:
    """Return {ticker: {YYYY-MM-DD: split_ratio}} from cache, or empty dict."""
    global _splits_cache
    if _splits_cache is not None:
        return _splits_cache
    if not SPLITS_PATH.exists():
        _splits_cache = {}
        return _splits_cache
    with _cache_lock:
        _splits_cache = _loads(SPLITS_PATH.read_bytes())
    return _splits_cache


def save_splits(splits: dict) -> None:
    """Persist {ticker: {YYYY-MM-DD: split_ratio}} cache."""
    global _splits_cache
    _splits_cache = splits
    with _cache_lock:
        _write_bytes_atomic(SPLITS_PATH, _dumps(splits).encode())
