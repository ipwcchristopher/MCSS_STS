#!/usr/bin/env bash
# MCSS Manual Run Script (local development)
# Usage: ./run_mcss.sh [--dry-run] [--session=pre-market|post-market]
# Auth:  claude auth login  (uses Claude.ai subscription, no API key needed)

set -e

DRY_RUN=""
SESSION="post-market"

for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN="true" ;;
    --session=*) SESSION="${arg#*=}" ;;
  esac
done

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
