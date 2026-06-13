"""
Telegram push helper for MCSS daily reports.
Usage: python scripts/send_telegram.py --message "..." [--dry-run]
Secrets from env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import os
import sys
import time
from pathlib import Path
from typing import List

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

TELEGRAM_LIMIT = 4096
_PART_PREFIX_RESERVE = 8  # room for "(9/9)\n" prefix added after splitting


def split_message(message: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """Split a message into Telegram-sized chunks. Pure function — unit-tested.

    Splits on report block separator lines (starting with ━) so HTML tags,
    which never span blocks, stay intact. A single oversized block falls back
    to splitting at the last newline inside the window.
    """
    if len(message) <= limit:
        return [message]

    budget = limit - _PART_PREFIX_RESERVE
    blocks: List[List[str]] = [[]]
    for line in message.split("\n"):
        if line.startswith("━") and blocks[-1]:
            blocks.append([line])
        else:
            blocks[-1].append(line)

    chunks: List[str] = []
    current = ""
    for block in blocks:
        text = "\n".join(block)
        candidate = text if not current else f"{current}\n{text}"
        if len(candidate) <= budget:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # Oversized single block: cut at last newline inside the budget
        while len(text) > budget:
            cut = text.rfind("\n", 0, budget)
            cut = cut if cut > 0 else budget
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        current = text
    if current:
        chunks.append(current)

    return [f"({i}/{len(chunks)})\n{c}" for i, c in enumerate(chunks, 1)]


def send_message(message: str, dry_run: bool = False) -> bool:
    """Send Telegram message (auto-split over 4096 chars). Prints in dry-run."""
    parts = split_message(message)

    if dry_run:
        for part in parts:
            print("=" * 50)
            print(f"DRY RUN — Telegram message (not sent, {len(part)} chars):")
            print("=" * 50)
            print(part)
            print("=" * 50)
        return True

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for i, part in enumerate(parts):
        if i > 0:
            time.sleep(0.5)  # stay clear of Telegram per-chat rate limits
        payload = {
            "chat_id": chat_id,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=30)
        if not resp.ok:
            # Surface Telegram's actual reason (e.g. "bot can't initiate conversation
            # with a user that hasn't started it") — the bare status line hides it.
            print(f"[send_telegram] Telegram API {resp.status_code}: {resp.text}",
                  file=sys.stderr)
        resp.raise_for_status()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Send MCSS report to Telegram")
    parser.add_argument("--message", required=True, help="Message text to send")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of sending")
    args = parser.parse_args()
    send_message(args.message, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
