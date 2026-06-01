#!/usr/bin/env bash
# MCSS Manual Run Script (local development)
# Usage:
#   ./run_mcss.sh [--dry-run] [--session=pre-market|post-market]
#   ./run_mcss.sh --backtest [--dry-run] [--refresh-cache]
# Auth:  claude auth login  (uses Claude.ai subscription, no API key needed)

set -e

DRY_RUN=""
SESSION="post-market"
BACKTEST=""
REFRESH_CACHE=""

for arg in "$@"; do
  case $arg in
    --dry-run)       DRY_RUN="true" ;;
    --session=*)     SESSION="${arg#*=}" ;;
    --backtest)      BACKTEST="true" ;;
    --refresh-cache) REFRESH_CACHE="true" ;;
  esac
done

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

claude -p "Run MCSS daily swing trade screen. Session: $SESSION. Today is $(date +%Y-%m-%d). Run the complete pipeline: mcss-market-gate → mcss-data-screener → [mcss-fundamental-analyst + mcss-technical-analyst in parallel] → mcss-quant-scorer → mcss-catalyst-analyst → mcss-reporter. All data files go to data/ directory. $DRY_RUN_INSTRUCTION" \
  --allowedTools "Bash,WebSearch,WebFetch,Agent" \
  --max-turns 50

echo "==================================="
echo "MCSS run complete."
echo "Check data/ directory for output files."
echo "==================================="
