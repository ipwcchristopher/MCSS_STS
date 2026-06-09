#!/usr/bin/env bash
# MCSS Manual Run Script (local development)
# Usage:
#   ./run_mcss.sh [--dry-run] [--session=pre-market|post-market]
#   ./run_mcss.sh --backtest [--dry-run] [--refresh-cache]
#   ./run_mcss.sh --bot          start Telegram bot (on-demand trigger)
# Auth:  claude auth login  (uses Claude.ai subscription, no API key needed)

set -e

# Load .env if present (local secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, etc.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck source=.env
  source "$SCRIPT_DIR/.env"
  set +a
fi

DRY_RUN=""
SESSION="post-market"
BACKTEST=""
REFRESH_CACHE=""
BOT=""

for arg in "$@"; do
  case $arg in
    --dry-run)       DRY_RUN="true" ;;
    --session=*)     SESSION="${arg#*=}" ;;
    --backtest)      BACKTEST="true" ;;
    --refresh-cache) REFRESH_CACHE="true" ;;
    --bot)           BOT="true" ;;
  esac
done

# ── Bot mode ─────────────────────────────────────────────────────────────────
if [ -n "$BOT" ]; then
  echo "==================================="
  echo "MCSS Telegram Bot"
  echo "Date: $(date +%Y-%m-%d)"
  echo "Send /run in Telegram to trigger pipeline."
  echo "Press Ctrl+C to stop."
  echo "==================================="
  .venv/bin/python scripts/telegram_bot.py
  exit 0
fi

# ── Backtest mode ────────────────────────────────────────────────────────────
if [ -n "$BACKTEST" ]; then
  echo "==================================="
  echo "MCSS Phase 5 — Backtesting Engine"
  echo "Date: $(date +%Y-%m-%d)"
  echo "==================================="
  ARGS=""
  [ -n "$DRY_RUN" ]       && ARGS="$ARGS --dry-run"
  [ -n "$REFRESH_CACHE" ] && ARGS="$ARGS --refresh-cache"
  .venv/bin/python scripts/backtest.py $ARGS
  echo "==================================="
  echo "Backtest complete. Check:"
  echo "  data/backtest_report.html"
  echo "  data/backtest_trades.csv"
  echo "==================================="
  exit 0
fi

echo "==================================="
echo "MCSS Daily Screen"
echo "Date:    $(date +%Y-%m-%d)"
echo "Session: $SESSION"
echo "Mode:    ${DRY_RUN:+dry-run}${DRY_RUN:-live}"
echo "==================================="

if ! claude auth status &>/dev/null; then
  echo "ERROR: Not logged in to Claude. Run:"
  echo "  claude auth login"
  exit 1
fi

DRY_RUN_INSTRUCTION=""
if [ -n "$DRY_RUN" ]; then
  DRY_RUN_INSTRUCTION="Use dry-run mode — print Telegram report to console, do not send."
fi

claude -p "Use the mcss-orchestrator agent to run the MCSS daily swing trade screen. Session: $SESSION. Today is $(date +%Y-%m-%d). The orchestrator runs the deterministic pipeline sequentially via Bash (scripts/market_gate.py -> fetch_universe.py -> fundamental_filter.py -> technical_filter.py -> quant_scoring.py), then spawns the mcss-catalyst-analyst sub-agent for L5, then runs scripts/report_agent.py to push the Telegram report. All data files go to data/ directory. $DRY_RUN_INSTRUCTION" \
  --allowedTools "Bash,WebSearch,WebFetch,Agent,Read" \
  --max-turns 50

echo "==================================="
echo "MCSS run complete."
echo "Check data/ directory for output files."
echo "==================================="
