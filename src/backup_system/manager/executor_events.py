"""Typed ingestion of executor events that update manager-owned durable state."""

from __future__ import annotations

from uuid import UUID

from backup_system.common.events import (
    DiskOfflineConfirmed,
    DiskOfflineFailed,
    KnownExecutorEvent,
    Progress,
    RestoreCompleted,
    RestoreTargetReady,
    RestoreVersionResolved,
    RunFinished,
    RunStarted,
    SmartObserved,
    SmartTestDiskFinished,
    SnapshotCreated,
    StageChanged,
)
from backup_system.manager.operations import OperationsRepository, RunResult
from backup_system.manager.smart_history import SmartComparison, SmartHistoryRepository


class ExecutorEventIngestor:
    def __init__(self, smart_history: SmartHistoryRepository) -> None:
        self._smart_history = smart_history

    def ingest(
        self, event: KnownExecutorEvent, *, run_id: UUID | None = None
    ) -> SmartComparison | None:
        if isinstance(event, SmartTestDiskFinished):
            if run_id is None:
                raise ValueError("SMART self-test result requires a run ID")
            with self._smart_history.connection:
                self._smart_history.connection.execute(
                    """INSERT INTO smart_test_results(
                        run_id, disk_id, identity_key, test_type, result, reason,
                        duration_seconds, remaining_percent, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(run_id), event.disk_id, event.identity_key, event.test_type,
                        event.result, event.reason, event.duration_seconds,
                        event.remaining_percent, event.timestamp.isoformat(),
                    ),
                )
            return None
        if not isinstance(event, SmartObserved):
            return None
        if event.collection_success and event.identity_key is None:
            raise ValueError("successful SMART event must contain an identity key")
        return self._smart_history.record(
            disk_id=event.disk_id,
            public_disk_id=event.disk_id,
            identity_key=event.identity_key or "unavailable",
            role="monitored",
            observed_at=event.timestamp,
            operational_state="unknown",
            smart_health=event.health,
            metrics=event.metrics,
            collection_success=event.collection_success,
        )


class ExecutorRunEventProcessor:
    def __init__(
        self,
        *,
        run_id: UUID,
        job_id: str,
        operations: OperationsRepository,
        smart: ExecutorEventIngestor,
    ) -> None:
        self._run_id = run_id
        self._job_id = job_id
        self._operations = operations
        self._smart = smart
        self._offline_confirmed: bool | None = None
        self._snapshot_id: str | None = None
        self._bytes_added: int | None = None
        self._resolved_restore_version: str | None = None

    def process(self, event: KnownExecutorEvent) -> SmartComparison | None:
        if isinstance(event, RunStarted):
            if event.run_id != self._run_id or event.job_id != self._job_id:
                raise ValueError("executor run identity does not match claimed run")
            return None
        if isinstance(event, StageChanged):
            self._operations.update_stage(self._run_id, event.stage, changed_at=event.timestamp)
            return None
        if isinstance(event, Progress):
            self._operations.update_progress(
                self._run_id,
                files_done=event.files_done,
                files_total=event.files_total,
                bytes_done=event.bytes_done,
                bytes_total=event.bytes_total,
                updated_at=event.timestamp,
            )
            return None
        if isinstance(event, SnapshotCreated):
            self._snapshot_id = event.snapshot_id
            self._bytes_added = event.bytes_added
            return None
        if isinstance(event, RestoreVersionResolved):
            if self._resolved_restore_version is not None:
                raise ValueError("executor emitted multiple restore resolutions")
            self._resolved_restore_version = event.version
            return None
        if isinstance(event, RestoreTargetReady):
            self._operations.set_restore_target(self._run_id, event.result_path)
            return None
        if isinstance(event, RestoreCompleted):
            self._operations.complete_restore_metrics(
                self._run_id,
                result_path=event.result_path,
                files_restored=event.files_restored,
                logical_bytes=event.logical_bytes,
            )
            return None
        if isinstance(event, SmartObserved):
            return self._smart.ingest(event, run_id=self._run_id)
        if isinstance(event, SmartTestDiskFinished):
            return self._smart.ingest(event, run_id=self._run_id)
        if isinstance(event, DiskOfflineConfirmed):
            self._set_offline_state(True)
            return None
        if isinstance(event, DiskOfflineFailed):
            self._set_offline_state(False)
            return None
        if isinstance(event, RunFinished):
            self._finish(event)
            return None
        raise TypeError(f"unsupported executor event type: {type(event).__name__}")

    def _set_offline_state(self, confirmed: bool) -> None:
        if self._offline_confirmed is not None and self._offline_confirmed is not confirmed:
            raise ValueError("executor emitted conflicting disk offline events")
        self._offline_confirmed = confirmed

    def _finish(self, event: RunFinished) -> None:
        if event.exit_code is None or event.disk_offline_confirmed is None:
            raise ValueError("executor terminal event is missing required outcome fields")
        if self._offline_confirmed is None:
            raise ValueError("executor terminal event arrived before disk offline outcome")
        if event.disk_offline_confirmed is not self._offline_confirmed:
            raise ValueError("executor terminal event conflicts with disk offline outcome")
        if event.result == "success" and not self._offline_confirmed:
            raise ValueError("successful executor result requires confirmed disk offline")
        if event.result == "success" and self._resolved_restore_version is None:
            row = self._operations.connection.execute(
                "SELECT kind FROM runs WHERE run_id = ?", (str(self._run_id),)
            ).fetchone()
            if row is not None and str(row[0]) == "resolve-restore":
                raise ValueError("successful resolver emitted no resolved version")
        self._operations.finish_run(
            self._run_id,
            result=RunResult(event.result),
            exit_code=event.exit_code,
            disk_offline_confirmed=self._offline_confirmed,
            snapshot_id=self._snapshot_id,
            resolved_restore_version=self._resolved_restore_version,
            bytes_added=self._bytes_added,
            finished_at=event.timestamp,
        )
