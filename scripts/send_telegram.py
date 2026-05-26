"""
Telegram push helper for MCSS daily reports.
Usage: python scripts/send_telegram.py --message "..." [--dry-run]
Secrets from env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import os

import requests


def send_message(message: str, dry_run: bool = False) -> bool:
    """Send Telegram message. Prints to console in dry-run mode."""
    if dry_run:
        print("=" * 50)
        print("DRY RUN — Telegram message (not sent):")
        print("=" * 50)
        print(message)
        print("=" * 50)
        return True

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
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
