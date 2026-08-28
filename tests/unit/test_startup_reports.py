import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backup_system.manager.database import open_manager_database
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import OperationsRepository
from backup_system.manager.scheduler_events import SchedulerEventRepository
from backup_system.manager.startup_reports import StartupReportPlanner


def test_startup_report_aggregates_and_marks_missed_events(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)
    events = SchedulerEventRepository(connection)
    planner = StartupReportPlanner(connection, NotificationRepository(connection))
    started = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
        with connection:
            for index, kind in enumerate(("backup", "check", "prune")):
                events.append_in_transaction(
                    deduplication_key=f"missed:{index}",
                    event_type="schedule_missed",
                    job_id="data",
                    operation_kind=kind,
                    scheduled_at=started - timedelta(hours=3 - index),
                    reason="manager_downtime",
                    created_at=started,
                )
        _, created = planner.enqueue(
            started_at=started,
            previous_seen_at=started - timedelta(hours=4),
            interrupted=("Data backup",),
            disk_issues=("backup disk state unknown",),
        )
        assert created
        payload = json.loads(
            str(connection.execute("SELECT payload_json FROM notifications").fetchone()[0])
        )
        assert payload["downtime_seconds"] == 14400
        assert len(payload["missed_backups"]) == 1
        assert len(payload["missed_checks"]) == 1
        assert payload["missed_other_count"] == 1
        assert connection.execute(
            "SELECT count(*) FROM scheduler_events WHERE startup_reported_at IS NOT NULL"
        ).fetchone() == (3,)
    finally:
        connection.close()


def test_poll_late_event_is_not_included_in_startup_report(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)
    events = SchedulerEventRepository(connection)
    started = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
        with connection:
            events.append_in_transaction(
                deduplication_key="late:data",
                event_type="schedule_missed",
                job_id="data",
                operation_kind="backup",
                scheduled_at=started,
                reason="poll_late",
                created_at=started,
            )
        StartupReportPlanner(connection, NotificationRepository(connection)).enqueue(
            started_at=started, previous_seen_at=started - timedelta(minutes=1)
        )
        payload = json.loads(
            str(connection.execute("SELECT payload_json FROM notifications").fetchone()[0])
        )
        assert payload["missed_backups"] == []
    finally:
        connection.close()
