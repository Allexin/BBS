"""Consume ignored plaintext credentials, provision DPAPI blobs, and test delivery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx

from backup_system.deployment.security import apply_administrative_acl
from backup_system.manager.telegram import TelegramSender
from backup_system.manager.telegram_credentials import (
    TelegramCredentials,
    load_telegram_credentials,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
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
    thread_id = _optional_thread(payload, "message_thread_id")
    credentials = TelegramCredentials(
        bot_token=token,
        chat_id=chat_id,
        message_thread_id=thread_id,
    )
    destination = arguments.destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    apply_administrative_acl(destination.parent)
    temporary = destination.parent / f".{destination.name}.{uuid4()}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(credentials.model_dump_json(indent=2).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        apply_administrative_acl(temporary)
        os.replace(temporary, destination)
        apply_administrative_acl(destination)
    finally:
        temporary.unlink(missing_ok=True)
    credentials_path.unlink()

    stored = load_telegram_credentials(destination.parent, destination.name)
    with httpx.Client(timeout=10) as client:
        TelegramSender(
            token=stored.bot_token,
            chat_id=stored.chat_id,
            message_thread_id=stored.message_thread_id,
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


def _optional_thread(payload: dict[object, object], key: str) -> int | None:
    value = payload.get(key)
    if value == "" or value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a positive integer or empty")
    try:
        result = int(value) if isinstance(value, (str, int)) else 0
    except ValueError as error:
        raise ValueError(f"{key} must be a positive integer or empty") from error
    if result <= 0:
        raise ValueError(f"{key} must be a positive integer or empty")
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Telegram setup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
