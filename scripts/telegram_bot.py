"""
MCSS Telegram Bot — on-demand pipeline trigger.

Commands:
  /run [pre-market|post-market]  run full pipeline (default: post-market)
  /status                        show last pipeline_run.json summary
  /help                          list available commands

Security: only responds to the configured TELEGRAM_CHAT_ID.
Concurrent runs are blocked — /run while pipeline is running returns a notice.

Usage:
  .venv/bin/python scripts/telegram_bot.py
  or:  ./run_mcss.sh --bot

Required env vars:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
SCRIPTS_DIR = ROOT / "scripts"

_is_running = False


def _authorized(update: Update) -> bool:
    try:
        return update.effective_chat.id == int(os.environ["TELEGRAM_CHAT_ID"])
    except (KeyError, ValueError):
        return False


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "📊 <b>MCSS Bot</b>\n\n"
        "/run — run post-market pipeline\n"
        "/run pre-market — run pre-market session\n"
        "/status — show last run result\n"
        "/help — this message",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return

    run_file = DATA_DIR / "pipeline_run.json"
    if not run_file.exists():
        await update.message.reply_text("No pipeline run found yet.")
        return

    try:
        data = json.loads(run_file.read_text())
    except Exception as e:
        await update.message.reply_text(f"Failed to read status: {e}")
        return

    result = data.get("result", "UNKNOWN")
    run_at = data.get("run_at", "?")
    session = data.get("session", "?")
    stages = data.get("stages", {})

    icon = "✅" if result == "COMPLETE" else ("⏹" if result == "HALT" else "❌")
    stage_lines = "\n".join(f"  {k}: {v}" for k, v in stages.items())
    text = (
        f"{icon} <b>Last Run: {result}</b>\n"
        f"Session: {session}\n"
        f"Time: {run_at}\n\n"
        f"Stages:\n{stage_lines}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _is_running
    if not _authorized(update):
        return

    if _is_running:
        await update.message.reply_text("⏳ Pipeline already running. Please wait.")
        return

    session = "post-market"
    if context.args and context.args[0] in ("pre-market", "post-market"):
        session = context.args[0]

    await update.message.reply_text(f"🚀 Starting MCSS pipeline (session: {session})...")
    _is_running = True

    loop = asyncio.get_event_loop()

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "run_pipeline.py"), f"--session={session}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    try:
        result = await loop.run_in_executor(None, _run)
        if result.returncode == 0:
            await update.message.reply_text("✅ Pipeline completed successfully!")
        else:
            stderr_tail = result.stderr[-800:].strip() if result.stderr else "(no output)"
            await update.message.reply_text(
                f"❌ Pipeline failed (exit {result.returncode})\n\n"
                f"<code>{stderr_tail}</code>",
                parse_mode="HTML",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Unexpected error: {e}")
    finally:
        _is_running = False


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN must be set")
    if not os.environ.get("TELEGRAM_CHAT_ID"):
        raise EnvironmentError("TELEGRAM_CHAT_ID must be set")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))

    print("MCSS Telegram Bot started. Send /run in Telegram to trigger pipeline.", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
