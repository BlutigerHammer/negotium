# ⚡ Negotium — Investment Tracker

A local-first portfolio tracker: follow the value of your stock portfolio over
time across USD, EUR, PLN positions — with broker statement imports, benchmark
comparisons, drawdown analysis, and a per-project watchlist. No server, no
database daemon, no background process: everything lives in plain JSON/JSONL
files on your disk.

Created by **Kacper Kaperek**. If you like this, buy me a coffee:
[buymeacoffee.com/ACCOUNTNUMBER](https://buymeacoffee.com/ACCOUNTNUMBER)

## Features

- **Multi-project** — keep broker accounts (XTB, BOSSA, …) as independent projects with separate ledgers
- **Multi-currency** — exchange suffix → currency mapping for 13 markets; cash tracked as positions; display in PLN / EUR / USD
- **Broker imports** — XTB Excel, BOSSA CSV, custom JSON; idempotent re-imports (duplicates skipped)
- **Ticker translation** — rewrite broker symbols to Yahoo Finance format with rules; resolve ISINs for BOSSA
- **Portfolio chart** — daily time-series with invested-capital line and benchmark overlays (S&P 500, NASDAQ 100, Semiconductors, FTSE All-World, EM, Bitcoin, Gold)
- **Metrics** — value, P&L, contributions, TWR, IRR, best performer
- **Analysis** — allocation donuts (sector / geography / asset class / currency), drawdown "underwater" chart, per-ticker trade history with dividends
- **Watchlist** — track any Yahoo Finance ticker with price, change and sparkline (per project)
- **Fast & local** — historical prices cached forever, only the current year re-fetches; everything except price downloads stays on localhost

## Quick start

**macOS / Linux:**
```bash
./start.sh
```

**Windows:** double-click `start.bat`, or run it from Command Prompt or PowerShell:

```powershell
.\start.bat
```

Both launchers find Python 3.10+, create a project-local `.venv`, install
missing dependencies, and start the app at http://localhost:8501. Tests are
skipped by default; use `--run-tests` to run them before launch.

| Option | Effect |
|---|---|
| `--run-tests` | run the test suite, then launch |
| `--tests-only` | run tests and exit |
| `--skip-tests` | skip tests on launch (default) |
| `--port N` | use port N (default 8501) |
| `--reset` | wipe all data and start fresh |
| `-h`, `--help` | show help |

## Requirements

- **Python 3.10+** (the launcher prefers 3.14 and falls back to 3.13 / 3.12 / 3.11 / 3.10)
- Internet access to **Yahoo Finance** for prices, FX rates, dividends and company metadata — the only outbound traffic; the UI itself is served on localhost
- Dependencies installed automatically on first run: `yfinance`, `streamlit`, `plotly`, `pandas`, `orjson`, `openpyxl`, `python-calamine`

## Broker ticker suffixes (XTB → Yahoo Finance)

Brokers name tickers differently from Yahoo Finance. **XTB uses country-code
suffixes** (`.PL`, `.UK`, `.NL`, …) while **Yahoo uses city / exchange codes**
(`.WA` Warsaw, `.L` London, `.AS` Amsterdam, …). The `ticker_rules` list in
`data/config.json` (editable in the UI under *Sidebar → ⚙️ Settings*) rewrites
symbols when transactions are entered or imported, so every position prices
correctly. Three rule forms:

| Form | Example | Meaning |
|------|---------|---------|
| Exact | `AMZN.DE=AMZ.DE` | rewrite one symbol |
| Suffix swap | `*.PL=*.WA` | keep base, swap suffix (`CDR.PL` → `CDR.WA`) |
| Strip | `*.US=` | drop the suffix (`AAPL.US` → `AAPL`) |

Rules run top-to-bottom, first match wins; unmatched symbols pass through
unchanged. When both sides use the same code no rule is needed (e.g. `.DE`
Xetra, `.HK` Hong Kong).

Current XTB → Yahoo mapping:

| XTB (country) | Yahoo (exchange) | Market | Example |
|---|---|---|---|
| `.US` | *(none)* | United States | `AAPL.US` → `AAPL` |
| `.UK` | `.L` | United Kingdom (London) | `SHEL.UK` → `SHEL.L` |
| `.PL` | `.WA` | Poland (Warsaw) | `CDR.PL` → `CDR.WA` |
| `.NL` | `.AS` | Netherlands (Amsterdam) | `ASML.NL` → `ASML.AS` |
| `.FR` | `.PA` | France (Paris) | `OR.FR` → `OR.PA` |
| `.ES` | `.MC` | Spain (Madrid) | `SAN.ES` → `SAN.MC` |
| `.IT` | `.MI` | Italy (Milan) | `ISP.IT` → `ISP.MI` |
| `.PT` | `.LS` | Portugal (Lisbon) | `EDP.PT` → `EDP.LS` |
| `.BE` | `.BR` | Belgium (Brussels) | `ABI.BE` → `ABI.BR` |
| `.AT` | `.VI` | Austria (Vienna) | `OMV.AT` → `OMV.VI` |
| `.CH` | `.SW` | Switzerland (Zurich) | `NESN.CH` → `NESN.SW` |
| `.IE` | `.IR` | Ireland (Dublin) | `BIR.IE` → `BIR.IR` |
| `.SE` | `.ST` | Sweden (Stockholm) | `VOLV-B.SE` → `VOLV-B.ST` |
| `.NO` | `.OL` | Norway (Oslo) | `EQNR.NO` → `EQNR.OL` |
| `.DK` | `.CO` | Denmark (Copenhagen) | `NOVO-B.DK` → `NOVO-B.CO` |
| `.FI` | `.HE` | Finland (Helsinki) | `NOKIA.FI` → `NOKIA.HE` |
| `.CZ` | `.PR` | Czechia (Prague) | `CEZ.CZ` → `CEZ.PR` |
| `.HU` | `.BD` | Hungary (Budapest) | `OTP.HU` → `OTP.BD` |
| `.GR` | `.AT` | Greece (Athens) | `HTO.GR` → `HTO.AT` |
| `.TR` | `.IS` | Türkiye (Istanbul) | `THYAO.TR` → `THYAO.IS` |
| `.JP` | `.T` | Japan (Tokyo) | `7203.JP` → `7203.T` |
| `.SG` | `.SI` | Singapore | `D05.SG` → `D05.SI` |
| `.DE` | `.DE` | Germany (Xetra) — same code, no rule | `SAP.DE` stays `SAP.DE` |
| `.HK` | `.HK` | Hong Kong — same code, no rule | `0700.HK` stays `0700.HK` |

Rules apply **at entry time** — transactions already in the ledger keep their
stored symbols (the Settings screen can re-apply rules, and unused rules are
harmless). The app infers each holding's price currency from the *Yahoo*
suffix, so after a swap the conversion is automatic (e.g. `.ST` → SEK,
`.L` → GBP). Full reference: [CONFIG.md](CONFIG.md#ticker-rules).

## Documentation

| Doc | Read it for |
|---|---|
| [USAGE.md](USAGE.md) | Daily use: projects, sidebar, transactions, dashboard, watchlist, refresh |
| [IMPORTS.md](IMPORTS.md) | Importing statements from XTB, BOSSA, or your own JSON |
| [CONFIG.md](CONFIG.md) | Settings reference: ticker rules, ISIN mappings, currencies, data files |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How it works inside: modules, data flow, caching, performance |

## Project layout

```
negotium/
├── start.sh                  ← launcher (finds python, installs deps, runs tests)
├── start.bat                 ← Windows launcher (same options as start.sh)
├── data/
│   ├── config.json           ← global config (all projects share it)
│   ├── projects.json         ← project registry (created_at, last_refresh, watchlist)
│   ├── prices/               ← shared price cache: prices/{TICKER}/{YEAR}.json
│   ├── ticker_names.json     ← company names per ticker
│   ├── ticker_meta.json      ← sector / country / asset class per ticker
│   ├── dividends.json        ← dividend history per ticker
│   ├── earnings.json         ← next earnings date per ticker
│   └── {PROJECT}/            ← one directory per project
│       ├── transactions.jsonl    ← ledger — the source of truth
│       ├── balance.json          ← derived current holdings
│       ├── portfolio.jsonl       ← derived daily snapshots
│       ├── benchmarks_{CCY}.json ← derived benchmark series
│       └── imports/{broker}/     ← uploaded statement files
├── src/                      ← application code (see ARCHITECTURE.md)
│   ├── app.py …              ← Streamlit entry + core modules
│   └── ui/                   ← Streamlit view layer, one module per section
└── tests/
    ├── conftest.py           ← shared fixtures (isolated temp dirs)
    └── test_*.py             ← 197 tests, pytest
```

## The 60-second data model

One line per calendar date in `transactions.jsonl` — positive amounts buy,
negative amounts sell (or spend cash):

```json
{"date": "2024-01-15", "entries": [{"ticker": "AAPL", "amount": 10.0}, {"ticker": "USD", "amount": -1710.0}]}
{"date": "2024-03-01", "entries": [{"ticker": "USD", "amount": -5000.0}, {"ticker": "PLN", "amount": 19850.0}]}
```

- `ticker` is a stock symbol or a cash currency (`USD` / `EUR` / `PLN`)
- the second line is a currency exchange: USD out, PLN in
- everything else in `data/` is derived from this ledger and can be rebuilt

Full format details: [CONFIG.md](CONFIG.md#data-files).
