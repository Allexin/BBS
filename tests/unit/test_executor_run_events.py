from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backup_system.common.events import (
    DiskOfflineConfirmed,
    DiskOfflineFailed,
    Progress,
    RunFinished,
    RunStarted,
    SnapshotCreated,
    StageChanged,
)
from backup_system.manager.database import open_manager_database
from backup_system.manager.executor_events import (
    ExecutorEventIngestor,
    ExecutorRunEventProcessor,
)
from backup_system.manager.operations import OperationsRepository
from backup_system.manager.smart_history import SmartHistoryRepository


def _processor(path: Path) -> tuple[ExecutorRunEventProcessor, object, object]:
    connection = open_manager_database(path)
    operations = OperationsRepository(connection)
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    operations.enqueue(
        deduplication_key="manual:test",
        job_id="data",
        kind="backup",
        trigger_source="manual",
    )
    run = operations.claim_next()
    assert run is not None
    processor = ExecutorRunEventProcessor(
        run_id=run.run_id,
        job_id=run.job_id,
        operations=operations,
        smart=ExecutorEventIngestor(SmartHistoryRepository(connection)),
    )
    return processor, run, connection


def test_event_sequence_updates_claimed_run_and_finishes_once(tmp_path: Path) -> None:
    processor, run, connection = _processor(tmp_path / "manager.sqlite3")
    now = datetime.now(UTC)
    try:
        processor.process(
            RunStarted(event="run_started", timestamp=now, run_id=run.run_id, job_id=run.job_id)
        )
        processor.process(StageChanged(event="stage_changed", timestamp=now, stage="backing_up"))
        processor.process(
            Progress(
                event="progress",
                timestamp=now,
                stage="backing_up",
                files_done=1,
                files_total=2,
            )
        )
        processor.process(
            SnapshotCreated(
                event="snapshot_created", timestamp=now, snapshot_id="snapshot-1", bytes_added=7
            )
        )
        processor.process(DiskOfflineConfirmed(event="disk_offline_confirmed", timestamp=now))
        processor.process(
            RunFinished(
                event="run_finished",
                timestamp=now + timedelta(seconds=1),
                result="success",
                exit_code=0,
                disk_offline_confirmed=True,
            )
        )
        assert connection.execute(
            "SELECT result, stage, files_done, snapshot_id, bytes_added, "
            "disk_offline_confirmed FROM runs"
        ).fetchone() == ("success", "backing_up", 1, "snapshot-1", 7, 1)
    finally:
        connection.close()


def test_terminal_without_prior_offline_event_is_rejected(tmp_path: Path) -> None:
    processor, _, connection = _processor(tmp_path / "manager.sqlite3")
    try:
        with pytest.raises(ValueError, match="before disk offline"):
            processor.process(
                RunFinished(
                    event="run_finished",
                    timestamp=datetime.now(UTC),
                    result="failed",
                    exit_code=30,
                    disk_offline_confirmed=False,
                )
            )
    finally:
        connection.close()


def test_conflicting_offline_events_are_rejected(tmp_path: Path) -> None:
    processor, _, connection = _processor(tmp_path / "manager.sqlite3")
    now = datetime.now(UTC)
    try:
        processor.process(DiskOfflineFailed(event="disk_offline_failed", timestamp=now))
        with pytest.raises(ValueError, match="conflicting"):
            processor.process(DiskOfflineConfirmed(event="disk_offline_confirmed", timestamp=now))
    finally:
        connection.close()
