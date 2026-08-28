"""Append-only durable events produced by scheduler decisions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from backup_system.common.time import require_aware, utc_now


class SchedulerEventRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def append_in_transaction(
        self,
        *,
        deduplication_key: str,
        event_type: str,
        job_id: str,
        operation_kind: str,
        scheduled_at: datetime,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> tuple[UUID, bool]:
        if not deduplication_key or not event_type:
            raise ValueError("scheduler event key and type must not be empty")
        event_id = uuid4()
        scheduled = _utc(scheduled_at).isoformat()
        created = _utc(created_at or utc_now()).isoformat()
        cursor = self._connection.execute(
            """INSERT INTO scheduler_events(
                event_id, deduplication_key, event_type, job_id, operation_kind,
                scheduled_at, reason, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deduplication_key) DO NOTHING""",
            (
                str(event_id),
                deduplication_key,
                event_type,
                job_id,
                operation_kind,
                scheduled,
                reason,
                json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                created,
            ),
        )
        if cursor.rowcount == 1:
            return event_id, True
        row = self._connection.execute(
            "SELECT event_id FROM scheduler_events WHERE deduplication_key = ?",
            (deduplication_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("deduplicated scheduler event could not be loaded")
        return UUID(str(row[0])), False


def _utc(value: datetime) -> datetime:
    return require_aware(value).astimezone(UTC)
