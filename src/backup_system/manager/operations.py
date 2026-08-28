"""Transactional persistence for jobs and the durable operation queue."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from backup_system.common.ids import new_operation_id, new_run_id
from backup_system.common.time import require_aware, utc_now
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.safety import SafetyLatchRepository


class OperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    REMOVED = "removed"
    DISCARDED_ON_RESTART = "discarded_on_restart"
    DISCARDED_ON_SERVICE_STOP = "discarded_on_service_stop"


class RunState(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"


class RunResult(StrEnum):
    SUCCESS = "success"
    WARNING = "warning"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


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
    existing_state: OperationState | None = None


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    run_id: UUID
    operation_id: UUID
    job_id: str
    kind: str
    mode: str | None


@dataclass(frozen=True, slots=True)
class StartupReconciliation:
    interrupted_run_ids: tuple[UUID, ...]
    discarded_operation_ids: tuple[UUID, ...]


class StateTransitionError(RuntimeError):
    """A requested durable state transition is not valid from the current state."""


class OperationsRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        notifications: NotificationRepository | None = None,
    ) -> None:
        self._connection = connection
        self._notifications = notifications

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
        request: dict[str, str] | None = None,
        scheduled_at: datetime | None = None,
        queued_at: datetime | None = None,
    ) -> EnqueueResult:
        with self._connection:
            return self.enqueue_in_transaction(
                deduplication_key=deduplication_key,
                job_id=job_id,
                kind=kind,
                trigger_source=trigger_source,
                mode=mode,
                request=request,
                scheduled_at=scheduled_at,
                queued_at=queued_at,
            )

    def enqueue_in_transaction(
        self,
        *,
        deduplication_key: str,
        job_id: str,
        kind: str,
        trigger_source: str,
        mode: str | None = None,
        request: dict[str, str] | None = None,
        scheduled_at: datetime | None = None,
        queued_at: datetime | None = None,
    ) -> EnqueueResult:
        """Enqueue without commit; the caller owns the surrounding transaction."""
        if not deduplication_key:
            raise ValueError("deduplication key must not be empty")
        operation_id = new_operation_id()
        queued = require_aware(queued_at or utc_now()).isoformat()
        scheduled = require_aware(scheduled_at).isoformat() if scheduled_at else None
        request_json = (
            json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if request is not None
            else None
        )
        try:
            self._connection.execute(
                """
                INSERT INTO operations(
                    operation_id, deduplication_key, job_id, kind, mode, request_json,
                    trigger_source, scheduled_at, queued_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(operation_id),
                    deduplication_key,
                    job_id,
                    kind,
                    mode,
                    request_json,
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
                """SELECT operation_id, state FROM operations
                WHERE job_id = ? AND kind = ? AND state IN ('queued', 'running')""",
                (job_id, kind),
            ).fetchone()
            if unfinished is not None:
                return EnqueueResult(
                    UUID(str(unfinished[0])),
                    EnqueueDisposition.COALESCED,
                    OperationState(str(unfinished[1])),
                )
            raise
        return EnqueueResult(operation_id, EnqueueDisposition.CREATED)

    def claim_next(self, *, started_at: datetime | None = None) -> ClaimedRun | None:
        timestamp = require_aware(started_at or utc_now()).isoformat()
        run_id = new_run_id()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if self._connection.execute(
                "SELECT 1 FROM operations WHERE state = ? LIMIT 1",
                (OperationState.RUNNING,),
            ).fetchone():
                self._connection.rollback()
                return None
            latch = SafetyLatchRepository(self._connection).active()
            if latch is None:
                row = self._connection.execute(
                    """SELECT operation_id, job_id, kind, mode
                    FROM operations WHERE state = ? ORDER BY queued_at, rowid LIMIT 1""",
                    (OperationState.QUEUED,),
                ).fetchone()
            else:
                row = self._connection.execute(
                    """SELECT operation_id, job_id, kind, mode FROM operations
                    WHERE state = ? AND job_id = ? AND kind = 'recover'
                        AND trigger_source = 'manual'
                    ORDER BY queued_at, rowid LIMIT 1""",
                    (OperationState.QUEUED, latch.job_id),
                ).fetchone()
            if row is None:
                self._connection.rollback()
                return None
            operation_id = UUID(str(row[0]))
            job_id = str(row[1])
            kind = str(row[2])
            mode = str(row[3]) if row[3] is not None else None
            cursor = self._connection.execute(
                "UPDATE operations SET state = ? WHERE operation_id = ? AND state = ?",
                (OperationState.RUNNING, str(operation_id), OperationState.QUEUED),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError("queued operation could not be claimed")
            self._connection.execute(
                """INSERT INTO runs(
                    run_id, operation_id, job_id, kind, state, started_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(run_id),
                    str(operation_id),
                    job_id,
                    kind,
                    RunState.RUNNING,
                    timestamp,
                ),
            )
            self._insert_event(
                run_id,
                timestamp,
                "run_started",
                {
                    "schema_version": 1,
                    "event": "run_started",
                    "run_id": str(run_id),
                    "job_id": job_id,
                    "timestamp": timestamp,
                },
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return ClaimedRun(run_id, operation_id, job_id, kind, mode)

    def update_stage(self, run_id: UUID, stage: str, *, changed_at: datetime | None = None) -> None:
        if not stage:
            raise ValueError("stage must not be empty")
        timestamp = require_aware(changed_at or utc_now()).isoformat()
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE runs SET stage = ?, stage_started_at = ?
                WHERE run_id = ? AND state = ?""",
                (stage, timestamp, str(run_id), RunState.RUNNING),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError("stage can be changed only for a running run")
            self._insert_event(
                run_id,
                timestamp,
                "stage_changed",
                {
                    "schema_version": 1,
                    "event": "stage_changed",
                    "stage": stage,
                    "timestamp": timestamp,
                },
            )

    def update_progress(
        self,
        run_id: UUID,
        *,
        files_done: int | None = None,
        files_total: int | None = None,
        bytes_done: int | None = None,
        bytes_total: int | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        values = (files_done, files_total, bytes_done, bytes_total)
        if any(value is not None and value < 0 for value in values):
            raise ValueError("progress values cannot be negative")
        if files_done is not None and files_total is not None and files_done > files_total:
            raise ValueError("files_done cannot exceed files_total")
        if bytes_done is not None and bytes_total is not None and bytes_done > bytes_total:
            raise ValueError("bytes_done cannot exceed bytes_total")
        timestamp = require_aware(updated_at or utc_now()).isoformat()
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE runs SET
                    progress_updated_at = ?, files_done = ?, files_total = ?,
                    bytes_done = ?, bytes_total = ?
                WHERE run_id = ? AND state = ?""",
                (
                    timestamp,
                    files_done,
                    files_total,
                    bytes_done,
                    bytes_total,
                    str(run_id),
                    RunState.RUNNING,
                ),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError("progress can be changed only for a running run")

    def finish_run(
        self,
        run_id: UUID,
        *,
        result: RunResult,
        exit_code: int,
        disk_offline_confirmed: bool,
        snapshot_id: str | None = None,
        bytes_added: int | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        timestamp = require_aware(finished_at or utc_now()).isoformat()
        if bytes_added is not None and bytes_added < 0:
            raise ValueError("bytes_added cannot be negative")
        with self._connection:
            row = self._connection.execute(
                """SELECT runs.operation_id, runs.deadline_at, runs.deadline_exceeded_at,
                    runs.job_id, operations.kind, operations.trigger_source
                FROM runs JOIN operations ON operations.operation_id = runs.operation_id
                WHERE runs.run_id = ? AND runs.state = ?""",
                (str(run_id), RunState.RUNNING),
            ).fetchone()
            if row is None:
                raise StateTransitionError("only a running run can be finished")
            operation_id = str(row[0])
            deadline_at = datetime.fromisoformat(str(row[1])) if row[1] is not None else None
            completed_time = datetime.fromisoformat(timestamp)
            overrun_seconds = (
                max(0, int((completed_time - deadline_at).total_seconds()))
                if deadline_at is not None
                else None
            )
            exceeded_at = (
                str(row[2])
                if row[2] is not None
                else deadline_at.isoformat()
                if deadline_at is not None and overrun_seconds is not None and overrun_seconds > 0
                else None
            )
            self._connection.execute(
                """UPDATE runs SET
                    state = ?, result = ?, finished_at = ?, exit_code = ?, snapshot_id = ?,
                    bytes_added = ?, disk_offline_confirmed = ?, deadline_exceeded_at = ?,
                    deadline_overrun_seconds = ?
                WHERE run_id = ?""",
                (
                    RunState.FINISHED,
                    result,
                    timestamp,
                    exit_code,
                    snapshot_id,
                    bytes_added,
                    int(disk_offline_confirmed),
                    exceeded_at,
                    overrun_seconds if exceeded_at is not None else None,
                    str(run_id),
                ),
            )
            cursor = self._connection.execute(
                "UPDATE operations SET state = ? WHERE operation_id = ? AND state = ?",
                (OperationState.COMPLETED, operation_id, OperationState.RUNNING),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError("run operation is not running")
            self._insert_event(
                run_id,
                timestamp,
                "run_finished",
                {
                    "schema_version": 1,
                    "event": "run_finished",
                    "result": result,
                    "timestamp": timestamp,
                },
            )
            if self._notifications is not None:
                display_row = self._connection.execute(
                    """SELECT display_name FROM jobs WHERE job_id =
                    (SELECT job_id FROM runs WHERE run_id = ?)""",
                    (str(run_id),),
                ).fetchone()
                display_name = str(display_row[0]) if display_row else "unknown"
                if result is RunResult.FAILED:
                    self._notifications.enqueue_in_transaction(
                        deduplication_key=f"run:{run_id}:failed",
                        run_id=run_id,
                        kind="run_failed",
                        payload={
                            "job": display_name,
                            "result": result,
                            "exit_code": exit_code,
                        },
                        created_at=completed_time,
                    )
                if not disk_offline_confirmed:
                    self._notifications.enqueue_in_transaction(
                        deduplication_key=f"run:{run_id}:disk-offline-unconfirmed",
                        run_id=run_id,
                        kind="disk_offline_unconfirmed",
                        payload={"job": display_name},
                        created_at=completed_time,
                    )
            if (
                result in {RunResult.SUCCESS, RunResult.WARNING}
                and disk_offline_confirmed
                and str(row[4]) == "recover"
                and str(row[5]) == "manual"
            ):
                SafetyLatchRepository(self._connection).clear_disk_lifecycle_in_transaction(
                    job_id=str(row[3]), cleared_at=completed_time
                )

    def reconcile_startup(self, *, reconciled_at: datetime | None = None) -> StartupReconciliation:
        timestamp = require_aware(reconciled_at or utc_now()).isoformat()
        with self._connection:
            rows = self._connection.execute(
                """SELECT run_id, operation_id, job_id, disk_offline_confirmed
                FROM runs WHERE state = ? ORDER BY started_at""",
                (RunState.RUNNING,),
            ).fetchall()
            for run_value, operation_value, job_value, offline_value in rows:
                run_id = UUID(str(run_value))
                cursor = self._connection.execute(
                    """UPDATE runs SET state = ?, result = ?, finished_at = ?
                    WHERE run_id = ? AND state = ?""",
                    (
                        RunState.FINISHED,
                        RunResult.INTERRUPTED,
                        timestamp,
                        str(run_id),
                        RunState.RUNNING,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateTransitionError("running run changed during reconciliation")
                cursor = self._connection.execute(
                    """UPDATE operations SET state = ?, terminal_reason = ?
                    WHERE operation_id = ? AND state = ?""",
                    (
                        OperationState.COMPLETED,
                        "manager_startup_interrupted",
                        str(operation_value),
                        OperationState.RUNNING,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateTransitionError("interrupted run operation is not running")
                self._insert_event(
                    run_id,
                    timestamp,
                    "run_finished",
                    {
                        "schema_version": 1,
                        "event": "run_finished",
                        "result": RunResult.INTERRUPTED,
                        "timestamp": timestamp,
                    },
                )
                if not bool(offline_value):
                    SafetyLatchRepository(self._connection).set_disk_lifecycle_in_transaction(
                        job_id=str(job_value),
                        source_run_id=run_id,
                        reason="manager_startup_interrupted_without_confirmed_offline",
                        created_at=datetime.fromisoformat(timestamp),
                    )
            queued_rows = self._connection.execute(
                "SELECT operation_id FROM operations WHERE state = ? ORDER BY queued_at, rowid",
                (OperationState.QUEUED,),
            ).fetchall()
            self._connection.execute(
                """UPDATE operations
                SET state = ?, removed_at = ?, terminal_reason = ? WHERE state = ?""",
                (
                    OperationState.DISCARDED_ON_RESTART,
                    timestamp,
                    "manager_restart",
                    OperationState.QUEUED,
                ),
            )
        return StartupReconciliation(
            interrupted_run_ids=tuple(UUID(str(row[0])) for row in rows),
            discarded_operation_ids=tuple(UUID(str(row[0])) for row in queued_rows),
        )

    def discard_queued_for_service_stop(
        self, *, discarded_at: datetime | None = None
    ) -> tuple[UUID, ...]:
        timestamp = require_aware(discarded_at or utc_now()).isoformat()
        with self._connection:
            rows = self._connection.execute(
                "SELECT operation_id FROM operations WHERE state = ? ORDER BY queued_at, rowid",
                (OperationState.QUEUED,),
            ).fetchall()
            self._connection.execute(
                """UPDATE operations
                SET state = ?, removed_at = ?, terminal_reason = ? WHERE state = ?""",
                (
                    OperationState.DISCARDED_ON_SERVICE_STOP,
                    timestamp,
                    "service_stopping",
                    OperationState.QUEUED,
                ),
            )
        return tuple(UUID(str(row[0])) for row in rows)

    def _insert_event(
        self,
        run_id: UUID,
        emitted_at: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        self._connection.execute(
            """INSERT INTO run_events(run_id, emitted_at, event_type, payload_json)
            VALUES (?, ?, ?, ?)""",
            (
                str(run_id),
                emitted_at,
                event_type,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )

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
