"""Consume ignored plaintext credentials, provision DPAPI blobs, and test delivery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from backup_system.manager.secret_provisioning import protect_secret
from backup_system.manager.secrets import load_manager_secret
from backup_system.manager.telegram import TelegramSender

TOKEN_ID = "telegram-bot-token"
CHAT_ID = "telegram-chat-id"
THREAD_ID = "telegram-thread-id"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--secret-directory", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args()
    credentials_path = arguments.credentials.resolve(strict=True)
    payload = json.loads(credentials_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "bot_token",
        "chat_id",
        "message_thread_id",
    }:
        raise ValueError("credentials file has an invalid schema")
    token = _required_text(payload, "bot_token")
    chat_id = _required_text(payload, "chat_id")
    thread_text = _optional_text(payload, "message_thread_id")
    thread_id = int(thread_text) if thread_text else None
    if thread_id is not None and thread_id <= 0:
        raise ValueError("message_thread_id must be a positive integer")

    protect_secret(
        arguments.secret_directory, TOKEN_ID, token, replace=True
    )
    protect_secret(
        arguments.secret_directory, CHAT_ID, chat_id, replace=True
    )
    if thread_text:
        protect_secret(
            arguments.secret_directory, THREAD_ID, thread_text, replace=True
        )
    credentials_path.unlink()

    protected_token = load_manager_secret(arguments.secret_directory, TOKEN_ID)
    protected_chat = load_manager_secret(arguments.secret_directory, CHAT_ID)
    with httpx.Client(timeout=10) as client:
        TelegramSender(
            token=protected_token,
            chat_id=protected_chat,
            message_thread_id=thread_id,
            client=client,
        ).send("BBS test notification: Telegram delivery is configured.")
    arguments.result.parent.mkdir(parents=True, exist_ok=True)
    arguments.result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "passed": True,
                "destination": "topic" if thread_id is not None else "chat",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Telegram test passed. Result saved to: {arguments.result}")
    return 0


def _required_text(payload: dict[object, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_text(payload: dict[object, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Telegram setup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
