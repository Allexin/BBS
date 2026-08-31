"""End-to-end synthetic failed-run notification through the real Telegram outbox."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from backup_system.common.time import utc_now
from backup_system.manager.database import open_manager_database
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import OperationsRepository, RunResult
from backup_system.manager.telegram import AsyncNotificationDispatcher, DispatchResult
from backup_system.manager.telegram_credentials import load_telegram_credentials


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    arguments = parser.parse_args()
    work = arguments.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    database = work / "manager.sqlite3"
    result_path = work / "result.json"
    database.unlink(missing_ok=True)
    result_path.unlink(missing_ok=True)

    credentials_path = arguments.credentials.resolve()
    credentials = load_telegram_credentials(credentials_path.parent, credentials_path.name)
    connection = open_manager_database(database)
    try:
        notifications = NotificationRepository(connection)
        operations = OperationsRepository(connection, notifications, managed_disk_jobs=set())
        operations.upsert_job(
            job_id="r6-telegram-failure",
            display_name="BBS R6 synthetic failure test",
            enabled=True,
            config_valid=True,
        )
        operations.enqueue(
            deduplication_key="r6:telegram-failure",
            job_id="r6-telegram-failure",
            kind="backup",
            trigger_source="manual",
        )
        run = operations.claim_next()
        if run is None:
            raise RuntimeError("synthetic operation was not claimed")
        operations.finish_run(
            run.run_id,
            result=RunResult.FAILED,
            exit_code=30,
            disk_offline_confirmed=True,
        )
    finally:
        connection.close()

    dispatcher = AsyncNotificationDispatcher(
        database,
        token=credentials.bot_token,
        chat_id=credentials.chat_id,
        message_thread_id=credentials.message_thread_id,
        proxy_url=credentials.proxy_url,
    )
    dispatch = asyncio.run(dispatcher.dispatch_one(now=utc_now()))
    if dispatch is not DispatchResult.SENT:
        raise RuntimeError(f"notification dispatch result is {dispatch}")

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT kind, state, attempts, last_error FROM notifications"
        ).fetchone()
    finally:
        connection.close()
    if row != ("run_failed", "sent", 0, None):
        raise RuntimeError("notification outbox terminal state is invalid")

    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "result": "success",
                "pipeline": "failed_run_to_telegram",
                "outbox_state": "sent",
                "destination": ("topic" if credentials.message_thread_id is not None else "chat"),
                "proxy_used": credentials.proxy_url is not None,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Telegram failure acceptance passed. Result saved to: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
