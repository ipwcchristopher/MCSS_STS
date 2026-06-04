#!/usr/bin/env bash
# MCSS Bot launcher — used by LaunchAgent (sources .env before starting bot)
set -e
cd /Users/chrisip/Projects/MCSS_STS
set -a
source .env
set +a
exec .venv/bin/python scripts/telegram_bot.py
