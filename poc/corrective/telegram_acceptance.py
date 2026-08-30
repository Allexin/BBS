"""Send one Telegram acceptance message using ignored Dev credentials."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from backup_system.manager.telegram import TelegramSender
from backup_system.manager.telegram_credentials import load_telegram_credentials


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args()
    credentials_path = arguments.credentials.absolute()
    credentials = load_telegram_credentials(
        credentials_path.parent, credentials_path.name
    )
    with httpx.Client(timeout=10, proxy=credentials.proxy_url) as client:
        TelegramSender(
            token=credentials.bot_token,
            chat_id=credentials.chat_id,
            message_thread_id=credentials.message_thread_id,
            client=client,
        ).send("BBS Dev test notification: Telegram delivery is configured.")
    arguments.result.parent.mkdir(parents=True, exist_ok=True)
    arguments.result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "passed": True,
                "destination": (
                    "topic" if credentials.message_thread_id is not None else "chat"
                ),
                "proxy_used": credentials.proxy_url is not None,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Telegram test passed. Result saved to: {arguments.result}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Telegram acceptance failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
