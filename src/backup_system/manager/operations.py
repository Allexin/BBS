"""Transactional persistence for jobs and the durable operation queue."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from backup_system.common.ids import new_operation_id
from backup_system.common.time import require_aware, utc_now


class OperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REMOVED = "removed"


class EnqueueDisposition(StrEnum):
    CREATED = "created"
    DEDUPLICATED = "deduplicated"
    COALESCED = "coalesced"


class RemoveDisposition(StrEnum):
    REMOVED = "removed"
    NOT_FOUND = "not_found"
    NOT_QUEUED = "not_queued"


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    operation_id: UUID
    disposition: EnqueueDisposition


class OperationsRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def upsert_job(
        self,
        *,
        job_id: str,
        display_name: str,
        enabled: bool,
        config_valid: bool,
        config_error: str | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        timestamp = require_aware(updated_at or utc_now()).isoformat()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO jobs(
                    job_id, display_name, enabled, config_valid, config_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    enabled = excluded.enabled,
                    config_valid = excluded.config_valid,
                    config_error = excluded.config_error,
                    updated_at = excluded.updated_at
                """,
                (job_id, display_name, int(enabled), int(config_valid), config_error, timestamp),
            )

    def enqueue(
        self,
        *,
        deduplication_key: str,
        job_id: str,
        kind: str,
        trigger_source: str,
        mode: str | None = None,
        scheduled_at: datetime | None = None,
        queued_at: datetime | None = None,
    ) -> EnqueueResult:
        if not deduplication_key:
            raise ValueError("deduplication key must not be empty")
        operation_id = new_operation_id()
        queued = require_aware(queued_at or utc_now()).isoformat()
        scheduled = require_aware(scheduled_at).isoformat() if scheduled_at else None
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO operations(
                        operation_id, deduplication_key, job_id, kind, mode,
                        trigger_source, scheduled_at, queued_at, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(operation_id),
                        deduplication_key,
                        job_id,
                        kind,
                        mode,
                        trigger_source,
                        scheduled,
                        queued,
                        OperationState.QUEUED,
                    ),
                )
        except sqlite3.IntegrityError:
            duplicate = self._connection.execute(
                "SELECT operation_id FROM operations WHERE deduplication_key = ?",
                (deduplication_key,),
            ).fetchone()
            if duplicate is not None:
                return EnqueueResult(UUID(str(duplicate[0])), EnqueueDisposition.DEDUPLICATED)
            unfinished = self._connection.execute(
                """SELECT operation_id FROM operations
                WHERE job_id = ? AND kind = ? AND state IN ('queued', 'running')""",
                (job_id, kind),
            ).fetchone()
            if unfinished is not None:
                return EnqueueResult(UUID(str(unfinished[0])), EnqueueDisposition.COALESCED)
            raise
        return EnqueueResult(operation_id, EnqueueDisposition.CREATED)

    def remove_queued(
        self, operation_id: UUID, *, removed_at: datetime | None = None
    ) -> RemoveDisposition:
        timestamp = require_aware(removed_at or utc_now()).isoformat()
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE operations
                SET state = ?, removed_at = ?, terminal_reason = ?
                WHERE operation_id = ? AND state = ?""",
                (
                    OperationState.REMOVED,
                    timestamp,
                    "manual_queue_remove",
                    str(operation_id),
                    OperationState.QUEUED,
                ),
            )
            if cursor.rowcount == 1:
                return RemoveDisposition.REMOVED
            existing = self._connection.execute(
                "SELECT 1 FROM operations WHERE operation_id = ?", (str(operation_id),)
            ).fetchone()
        return RemoveDisposition.NOT_QUEUED if existing else RemoveDisposition.NOT_FOUND
