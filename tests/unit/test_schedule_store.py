import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backup_system.common.config import ScheduleConfig
from backup_system.manager.database import open_manager_database
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import (
    EnqueueDisposition,
    EnqueueResult,
    OperationsRepository,
)
from backup_system.manager.schedule_store import ScheduleStore
from backup_system.manager.scheduler_events import SchedulerEventRepository


def _schedule() -> ScheduleConfig:
    return ScheduleConfig.model_validate(
        {
            "cron": "0 0 * * *",
            "timezone": "UTC",
            "deadline": "08:00",
            "cycle": [
                {"operation": "backup"},
                {"operation": "check", "mode": "subset"},
            ],
        }
    )


def _store(path: Path) -> tuple[ScheduleStore, OperationsRepository, sqlite3.Connection]:
    connection = open_manager_database(path)
    operations = OperationsRepository(connection)
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    return ScheduleStore(connection, operations), operations, connection


def test_new_job_starts_at_first_cycle_slot_without_catchup(tmp_path: Path) -> None:
    store, _, connection = _store(tmp_path / "manager.sqlite3")
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        store.initialize_new_job("data", _schedule(), now=now)
        row = connection.execute(
            """SELECT slot_counter, recovery_check_required, next_fire_at
            FROM schedule_state WHERE job_id = 'data'"""
        ).fetchone()
        assert row == (0, 0, datetime(2026, 8, 29, tzinfo=UTC).isoformat())
    finally:
        connection.close()


def test_startup_records_missed_fires_without_enqueuing(tmp_path: Path) -> None:
    _, operations, connection = _store(tmp_path / "manager.sqlite3")
    store = ScheduleStore(connection, operations, SchedulerEventRepository(connection))
    schedule = _schedule()
    stored_fire = datetime(2026, 8, 27, tzinfo=UTC)
    try:
        store.initialize_new_job("data", schedule, now=datetime(2026, 8, 26, 12, tzinfo=UTC))
        with connection:
            connection.execute(
                "UPDATE schedule_state SET next_fire_at = ? WHERE job_id = 'data'",
                (stored_fire.isoformat(),),
            )

        result = store.reconcile_startup(
            "data", schedule, now=datetime(2026, 8, 28, 12, tzinfo=UTC)
        )

        assert result.missed_at == (stored_fire, stored_fire + timedelta(days=1))
        assert result.phase.operation == "backup"
        assert connection.execute("SELECT count(*) FROM operations").fetchone() == (0,)
        assert connection.execute(
            "SELECT event_type, operation_kind FROM scheduler_events"
        ).fetchall() == [("schedule_missed", "backup"), ("schedule_missed", "backup")]
    finally:
        connection.close()


def test_startup_recalculates_next_fire_when_cron_changes(tmp_path: Path) -> None:
    store, _, connection = _store(tmp_path / "manager.sqlite3")
    old_schedule = _schedule()
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        store.initialize_new_job("data", old_schedule, now=now)
        changed = ScheduleConfig.model_validate(
            {
                "cron": "0 5 * * 6",
                "timezone": "UTC",
                "deadline": "08:00",
                "cycle": old_schedule.cycle,
            }
        )
        result = store.reconcile_startup(
            "data", changed, now=datetime(2026, 8, 30, 15, tzinfo=UTC)
        )

        assert result.missed_at == ()
        assert result.next_fire_at == datetime(2026, 9, 5, 5, tzinfo=UTC)
        assert connection.execute("SELECT count(*) FROM scheduler_events").fetchone() == (0,)
    finally:
        connection.close()


def test_missing_state_requires_recovery_check(tmp_path: Path) -> None:
    store, _, connection = _store(tmp_path / "manager.sqlite3")
    try:
        result = store.reconcile_startup(
            "data", _schedule(), now=datetime(2026, 8, 28, 12, tzinfo=UTC)
        )
        assert result.state_was_missing
        assert result.phase.operation == "check"
        assert result.phase.mode == "full"
        assert connection.execute(
            "SELECT slot_counter, recovery_check_required FROM schedule_state"
        ).fetchone() == (0, 1)
    finally:
        connection.close()


def test_poll_atomically_enqueues_due_phase_and_advances_fire(tmp_path: Path) -> None:
    store, _, connection = _store(tmp_path / "manager.sqlite3")
    schedule = _schedule()
    due = datetime(2026, 8, 28, tzinfo=UTC)
    try:
        store.initialize_new_job("data", schedule, now=datetime(2026, 8, 27, 12, tzinfo=UTC))
        result = store.poll("data", schedule, now=due + timedelta(seconds=4), poll_seconds=5)

        assert result.evaluation.due_at == due
        assert result.enqueue_result is not None
        assert result.enqueue_result.disposition is EnqueueDisposition.CREATED
        assert connection.execute(
            """SELECT kind, mode, trigger_source, scheduled_at, state
            FROM operations"""
        ).fetchone() == ("backup", None, "scheduled", due.isoformat(), "queued")
        assert connection.execute(
            "SELECT next_fire_at FROM schedule_state WHERE job_id = 'data'"
        ).fetchone() == ((due + timedelta(days=1)).isoformat(),)
    finally:
        connection.close()


def test_completion_advances_only_eligible_cycle_phase(tmp_path: Path) -> None:
    store, _, connection = _store(tmp_path / "manager.sqlite3")
    schedule = _schedule()
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        store.initialize_new_job("data", schedule, now=now)
        unchanged = store.record_completion(
            "data",
            schedule,
            result="failed",
            trigger_source="scheduled",
            operation="backup",
            check_mode=None,
            completed_at=now,
        )
        advanced = store.record_completion(
            "data",
            schedule,
            result="success",
            trigger_source="scheduled",
            operation="backup",
            check_mode=None,
            completed_at=now,
        )
        assert unchanged.slot_counter == 0
        assert advanced.slot_counter == 1
    finally:
        connection.close()


class _FailingOperations(OperationsRepository):
    def enqueue_in_transaction(self, **kwargs: object) -> EnqueueResult:
        raise RuntimeError("simulated enqueue failure")


def test_failed_enqueue_does_not_advance_schedule_state(tmp_path: Path) -> None:
    _, _, connection = _store(tmp_path / "manager.sqlite3")
    schedule = _schedule()
    due = datetime(2026, 8, 28, tzinfo=UTC)
    store = ScheduleStore(connection, _FailingOperations(connection))
    try:
        store.initialize_new_job("data", schedule, now=datetime(2026, 8, 27, 12, tzinfo=UTC))
        before = connection.execute(
            "SELECT last_evaluated_at, next_fire_at FROM schedule_state"
        ).fetchone()
        with pytest.raises(RuntimeError, match="simulated enqueue failure"):
            store.poll("data", schedule, now=due, poll_seconds=5)
        after = connection.execute(
            "SELECT last_evaluated_at, next_fire_at FROM schedule_state"
        ).fetchone()
        assert after == before
        assert connection.execute("SELECT count(*) FROM operations").fetchone() == (0,)
    finally:
        connection.close()


def test_coalesced_trigger_records_reason_without_overlap_alert(tmp_path: Path) -> None:
    _, operations, connection = _store(tmp_path / "manager.sqlite3")
    events = SchedulerEventRepository(connection)
    store = ScheduleStore(connection, operations, events)
    schedule = _schedule()
    due = datetime(2026, 8, 28, tzinfo=UTC)
    try:
        store.initialize_new_job("data", schedule, now=datetime(2026, 8, 27, 12, tzinfo=UTC))
        operations.enqueue(
            deduplication_key="manual:existing",
            job_id="data",
            kind="backup",
            trigger_source="manual",
            queued_at=due,
        )
        result = store.poll("data", schedule, now=due, poll_seconds=5)
        assert result.enqueue_result is not None
        assert result.enqueue_result.disposition is EnqueueDisposition.COALESCED
        assert connection.execute("SELECT event_type, reason FROM scheduler_events").fetchone() == (
            "duplicate_trigger_skipped",
            "already_queued",
        )
        assert connection.execute("SELECT count(*) FROM notifications").fetchone() == (0,)
    finally:
        connection.close()


def test_new_scheduled_work_behind_other_run_creates_one_overlap_alert(
    tmp_path: Path,
) -> None:
    _, operations, connection = _store(tmp_path / "manager.sqlite3")
    events = SchedulerEventRepository(connection)
    notifications = NotificationRepository(connection)
    store = ScheduleStore(connection, operations, events, notifications)
    schedule = _schedule()
    due = datetime(2026, 8, 28, tzinfo=UTC)
    try:
        operations.upsert_job(
            job_id="photos", display_name="Photos", enabled=True, config_valid=True
        )
        operations.enqueue(
            deduplication_key="photos:running",
            job_id="photos",
            kind="backup",
            trigger_source="scheduled",
            queued_at=due - timedelta(hours=1),
        )
        running = operations.claim_next(started_at=due - timedelta(minutes=30))
        assert running is not None
        operations.update_stage(running.run_id, "backup", changed_at=due - timedelta(minutes=20))
        store.initialize_new_job("data", schedule, now=datetime(2026, 8, 27, 12, tzinfo=UTC))

        result = store.poll("data", schedule, now=due, poll_seconds=5)

        assert result.enqueue_result is not None
        assert result.enqueue_result.disposition is EnqueueDisposition.CREATED
        assert connection.execute("SELECT event_type FROM scheduler_events").fetchall() == [
            ("schedule_overlap",)
        ]
        assert connection.execute("SELECT kind FROM notifications").fetchall() == [
            ("schedule_overlap",)
        ]
    finally:
        connection.close()
