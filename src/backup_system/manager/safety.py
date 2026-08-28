"""Persistent global safety latches that gate executor work after unsafe cleanup."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from backup_system.common.time import require_aware

DISK_LIFECYCLE_LATCH = "global:disk-lifecycle"


@dataclass(frozen=True, slots=True)
class ActiveSafetyLatch:
    latch_key: str
    latch_type: str
    job_id: str
    source_run_id: UUID
    reason: str
    created_at: datetime


class SafetyLatchRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def active(self) -> ActiveSafetyLatch | None:
        row = self._connection.execute(
            """SELECT latch_key, latch_type, job_id, source_run_id, reason, created_at
            FROM safety_latches WHERE cleared_at IS NULL ORDER BY created_at LIMIT 1"""
        ).fetchone()
        if row is None:
            return None
        return ActiveSafetyLatch(
            latch_key=str(row[0]),
            latch_type=str(row[1]),
            job_id=str(row[2]),
            source_run_id=UUID(str(row[3])),
            reason=str(row[4]),
            created_at=datetime.fromisoformat(str(row[5])),
        )

    def set_disk_lifecycle_in_transaction(
        self, *, job_id: str, source_run_id: UUID, reason: str, created_at: datetime
    ) -> None:
        timestamp = require_aware(created_at).isoformat()
        self._connection.execute(
            """INSERT INTO safety_latches(
                latch_key, latch_type, job_id, source_run_id, reason, created_at, cleared_at
            ) VALUES (?, 'disk_lifecycle', ?, ?, ?, ?, NULL)
            ON CONFLICT(latch_key) DO UPDATE SET
                job_id = excluded.job_id,
                source_run_id = excluded.source_run_id,
                reason = excluded.reason,
                created_at = excluded.created_at,
                cleared_at = NULL
            WHERE safety_latches.cleared_at IS NOT NULL""",
            (DISK_LIFECYCLE_LATCH, job_id, str(source_run_id), reason, timestamp),
        )

    def clear_disk_lifecycle_in_transaction(self, *, job_id: str, cleared_at: datetime) -> bool:
        timestamp = require_aware(cleared_at).isoformat()
        cursor = self._connection.execute(
            """UPDATE safety_latches SET cleared_at = ?
            WHERE latch_key = ? AND job_id = ? AND cleared_at IS NULL""",
            (timestamp, DISK_LIFECYCLE_LATCH, job_id),
        )
        return cursor.rowcount == 1
