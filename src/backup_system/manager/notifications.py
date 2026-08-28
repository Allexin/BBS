"""Durable, deduplicated notification outbox independent of job results."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from backup_system.common.time import require_aware, utc_now


class NotificationState(StrEnum):
    PENDING = "pending"
    SENT = "sent"


@dataclass(frozen=True, slots=True)
class PendingNotification:
    notification_id: UUID
    deduplication_key: str
    run_id: UUID | None
    kind: str
    payload: dict[str, Any]
    attempts: int


class NotificationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def enqueue(
        self,
        *,
        deduplication_key: str,
        kind: str,
        payload: dict[str, Any],
        run_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> tuple[UUID, bool]:
        if not deduplication_key or not kind:
            raise ValueError("notification key and kind must not be empty")
        notification_id = uuid4()
        timestamp = _utc(created_at or utc_now()).isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._connection:
            cursor = self._connection.execute(
                """INSERT INTO notifications(
                    notification_id, deduplication_key, run_id, kind, payload_json,
                    state, created_at, next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(deduplication_key) DO NOTHING""",
                (
                    str(notification_id),
                    deduplication_key,
                    str(run_id) if run_id else None,
                    kind,
                    payload_json,
                    NotificationState.PENDING,
                    timestamp,
                    timestamp,
                ),
            )
            if cursor.rowcount == 1:
                return notification_id, True
            row = self._connection.execute(
                "SELECT notification_id FROM notifications WHERE deduplication_key = ?",
                (deduplication_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("deduplicated notification could not be loaded")
            return UUID(str(row[0])), False

    def next_due(self, *, now: datetime | None = None) -> PendingNotification | None:
        timestamp = _utc(now or utc_now()).isoformat()
        row = self._connection.execute(
            """SELECT notification_id, deduplication_key, run_id, kind,
                payload_json, attempts
            FROM notifications
            WHERE state = ? AND next_attempt_at <= ?
            ORDER BY next_attempt_at, created_at, rowid LIMIT 1""",
            (NotificationState.PENDING, timestamp),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[4]))
        if not isinstance(payload, dict):
            raise RuntimeError("notification payload must be a JSON object")
        return PendingNotification(
            notification_id=UUID(str(row[0])),
            deduplication_key=str(row[1]),
            run_id=UUID(str(row[2])) if row[2] is not None else None,
            kind=str(row[3]),
            payload=payload,
            attempts=int(row[5]),
        )

    def record_failure(
        self,
        notification_id: UUID,
        error: str,
        *,
        failed_at: datetime | None = None,
    ) -> datetime:
        timestamp = _utc(failed_at or utc_now())
        with self._connection:
            row = self._connection.execute(
                "SELECT attempts FROM notifications WHERE notification_id = ? AND state = ?",
                (str(notification_id), NotificationState.PENDING),
            ).fetchone()
            if row is None:
                raise RuntimeError("only a pending notification can fail")
            attempts = int(row[0]) + 1
            delay = timedelta(seconds=min(3600, 30 * (2 ** min(attempts - 1, 7))))
            next_attempt = timestamp + delay
            self._connection.execute(
                """UPDATE notifications SET attempts = ?, last_error = ?,
                    next_attempt_at = ? WHERE notification_id = ?""",
                (attempts, error[:2000], next_attempt.isoformat(), str(notification_id)),
            )
        return next_attempt

    def record_sent(self, notification_id: UUID, *, sent_at: datetime | None = None) -> None:
        timestamp = _utc(sent_at or utc_now()).isoformat()
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE notifications SET state = ?, sent_at = ?, next_attempt_at = NULL,
                    last_error = NULL WHERE notification_id = ? AND state = ?""",
                (
                    NotificationState.SENT,
                    timestamp,
                    str(notification_id),
                    NotificationState.PENDING,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("only a pending notification can be sent")


def _utc(value: datetime) -> datetime:
    return require_aware(value).astimezone(UTC)
