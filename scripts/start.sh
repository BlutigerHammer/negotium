#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#   Negotium - Investment Tracker — launcher
#   Usage:  ./start.sh                 start UI (skip tests)
#          ./start.sh --run-tests      run tests then start UI
#          ./start.sh --tests-only     run tests only
#          ./start.sh --skip-tests     skip tests (default)
#          ./start.sh --port 8502     custom port (default 8501)
#          ./start.sh --reset         wipe all data and start fresh
# ─────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

RUN_TESTS=false
TESTS_ONLY=false
RESET=false
PORT=8501

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-tests)   RUN_TESTS=true   ; shift ;;
    --tests-only)  TESTS_ONLY=true  ; shift ;;
    --skip-tests)  RUN_TESTS=false  ; shift ;;
    --reset)       RESET=true       ; shift ;;
    --port)        PORT="$2"        ; shift 2 ;;
    -h|--help)     grep '^#  ' "$0" | sed 's/#  //'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

BOLD='\033[1m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; RESET_C='\033[0m'

# ── Find Python ───────────────────────────────────────────────
# NOTE: Python 3.14 is preferred but is a very new release; some versions of
# the Streamlit/yfinance stack print a noisy-but-harmless shutdown message on
# Ctrl+C ("I/O operation on closed file." / "lost sys.stderr"). Drop
# `python3.14` from the list below to fall back to the more conservative
# python3.13 runtime instead.
PYTHON=""
for cmd in python3.14 python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cmd" &>/dev/null; then
    VER=$("$cmd" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null || echo "False")
    if [[ "$VER" == "True" ]]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  echo -e "${RED}✗ Python 3.10+ not found.${RESET_C}"
  exit 1
fi

echo ""
echo -e "${BOLD}═══════════════════════════════════════════════${RESET_C}"
echo -e "${BOLD}  📈  Negotium - Investment Tracker${RESET_C}"
echo -e "${BOLD}═══════════════════════════════════════════════${RESET_C}"

# ── Optional reset ────────────────────────────────────────────
if [[ "$RESET" == "true" ]]; then
  echo ""
  echo -e "${YELLOW}  --reset: removing all data files…${RESET_C}"
  rm -rf data/
  echo -e "${GREEN}  ✓ Data cleared. Starting fresh.${RESET_C}"
fi

# ── Ensure required directories exist ────────────────────────
mkdir -p data

# ── Virtualenv ────────────────────────────────────────────────
# Prefer the project venv; create it on first run. This avoids pip-installing
# into the system interpreter (no more --break-system-packages).
VENV_DIR="$ROOT_DIR/.venv"
if [[ -x "$VENV_DIR/bin/python" ]]; then
  PYTHON="$VENV_DIR/bin/python"
else
  echo ""
  echo -e "  ${YELLOW}Creating virtualenv at .venv (first run)…${RESET_C}"
  "$PYTHON" -m venv "$VENV_DIR"
  PYTHON="$VENV_DIR/bin/python"
  echo -e "  ${YELLOW}Installing dependencies from requirements.txt…${RESET_C}"
  "$PYTHON" -m pip install --quiet --upgrade pip
  "$PYTHON" -m pip install --quiet -r "$ROOT_DIR/requirements.txt"
  echo -e "  ${GREEN}✓ Virtualenv ready${RESET_C}"
fi

echo ""
echo -e "  Python:  $($PYTHON --version)"
echo -e "  Dir:     $ROOT_DIR"

# ── Check dependencies (existing venvs may predate a new requirements.txt) ───
MISSING=()
for pkg in streamlit yfinance plotly pandas orjson openpyxl python_calamine pytest; do
  if ! "$PYTHON" -c "import $pkg" &>/dev/null 2>&1; then
    MISSING+=("$pkg")
  fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo -e "  ${YELLOW}Installing missing dependencies: ${MISSING[*]}${RESET_C}"
  "$PYTHON" -m pip install --quiet -r "$ROOT_DIR/requirements.txt"
fi
echo ""

# ── Run tests ─────────────────────────────────────────────────
if [[ "$RUN_TESTS" == "true" || "$TESTS_ONLY" == "true" ]]; then
  echo -e "${BOLD}Running tests…${RESET_C}"
  echo ""
  if "$PYTHON" -m pytest tests/; then
    echo ""
  else
    echo ""
    echo -e "${RED}✗ Tests failed.${RESET_C}"
    exit 1
  fi
fi

[[ "$TESTS_ONLY" == "true" ]] && { echo "Tests complete."; exit 0; }

# ── Launch ────────────────────────────────────────────────────
echo -e "${BOLD}Starting app → http://localhost:${PORT}${RESET_C}"
echo -e "  Press ${BOLD}Ctrl+C${RESET_C} to stop."
echo ""

exec "$PYTHON" -m streamlit run src/app_local.py \
  --server.port "$PORT" \
  --server.headless true \
  --server.runOnSave true \
  --browser.gatherUsageStats false
