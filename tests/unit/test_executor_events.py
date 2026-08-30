from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backup_system.common.events import SmartObserved, SmartTestDiskFinished, StageChanged
from backup_system.common.smart import SmartMetrics
from backup_system.manager.database import open_manager_database
from backup_system.manager.executor_events import ExecutorEventIngestor
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import OperationsRepository
from backup_system.manager.smart_history import SmartHistoryRepository


def _event(
    *, pending: int, observed_at: datetime, identity: str | None = "a" * 64
) -> SmartObserved:
    return SmartObserved(
        event="smart_observed",
        timestamp=observed_at,
        disk_id="source-main",
        collection_success=identity is not None,
        health="healthy" if identity is not None else "unknown",
        identity_key=identity,
        metrics=SmartMetrics(pending_sectors=pending),
        reason=None if identity is not None else "collection failed",
    )


def test_smart_events_create_baseline_then_durable_regression(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    try:
        ingestor = ExecutorEventIngestor(
            SmartHistoryRepository(connection, NotificationRepository(connection))
        )
        now = datetime.now(UTC)
        baseline = ingestor.ingest(_event(pending=0, observed_at=now))
        regression = ingestor.ingest(_event(pending=1, observed_at=now + timedelta(seconds=1)))
        assert baseline is not None and baseline.baseline_created
        assert regression is not None
        assert [item.rule_id for item in regression.regressions] == ["pending_sectors"]
        assert connection.execute("SELECT kind FROM notifications").fetchall() == [
            ("smart_regression",)
        ]
    finally:
        connection.close()


def test_unknown_observation_does_not_replace_successful_baseline(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    try:
        ingestor = ExecutorEventIngestor(SmartHistoryRepository(connection))
        now = datetime.now(UTC)
        ingestor.ingest(_event(pending=0, observed_at=now))
        unknown = ingestor.ingest(
            _event(pending=99, observed_at=now + timedelta(seconds=1), identity=None)
        )
        regression = ingestor.ingest(_event(pending=1, observed_at=now + timedelta(seconds=2)))
        assert unknown is not None and unknown.regressions == ()
        assert regression is not None
        assert [item.rule_id for item in regression.regressions] == ["pending_sectors"]
    finally:
        connection.close()


def test_same_identity_under_new_discovery_id_is_merged(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    try:
        ingestor = ExecutorEventIngestor(SmartHistoryRepository(connection))
        now = datetime.now(UTC)
        first = _event(pending=0, observed_at=now)
        ingestor.ingest(first)
        ingestor.ingest(
            first.model_copy(
                update={"disk_id": "system-disk-4", "timestamp": now + timedelta(seconds=1)}
            )
        )
        assert connection.execute("SELECT COUNT(*) FROM physical_disks").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM disk_observations").fetchone() == (2,)
        assert connection.execute("SELECT public_disk_id FROM physical_disks").fetchone() == (
            f"disk-{'a' * 12}",
        )
    finally:
        connection.close()


def test_success_without_identity_is_rejected() -> None:
    event = SmartObserved(
        event="smart_observed",
        timestamp=datetime.now(UTC),
        disk_id="source-main",
        collection_success=True,
        health="healthy",
        metrics=SmartMetrics(),
    )
    ingestor = ExecutorEventIngestor(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="identity key"):
        ingestor.ingest(event)


def test_unrelated_event_does_not_touch_smart_history() -> None:
    ingestor = ExecutorEventIngestor(None)  # type: ignore[arg-type]
    assert (
        ingestor.ingest(
            StageChanged(event="stage_changed", timestamp=datetime.now(UTC), stage="backing_up")
        )
        is None
    )


def test_self_test_result_is_bound_to_run(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)
    now = datetime.now(UTC)
    operations.upsert_job(job_id="smart", display_name="SMART", enabled=True, config_valid=True)
    operations.enqueue(
        deduplication_key="smart:test", job_id="smart", kind="smart-test",
        trigger_source="manual", queued_at=now,
    )
    run = operations.claim_next(started_at=now)
    assert run is not None
    ingestor = ExecutorEventIngestor(SmartHistoryRepository(connection))
    ingestor.ingest(
        SmartTestDiskFinished(
            event="smart_test_disk_finished", timestamp=now, disk_id="system-disk-1",
            identity_key="a" * 64, test_type="short", result="timeout",
            reason="SMART self-test completion timed out", duration_seconds=900,
            remaining_percent=90,
        ),
        run_id=run.run_id,
    )
    assert connection.execute(
        "SELECT result, remaining_percent FROM smart_test_results"
    ).fetchone() == ("timeout", 90)
    connection.close()
