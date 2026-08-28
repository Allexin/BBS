import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backup_system.manager.daily_reports import DailyReportStore
from backup_system.manager.database import open_manager_database
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import OperationsRepository, RunResult


def test_startup_skips_missed_daily_reports(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    store = DailyReportStore(connection, NotificationRepository(connection))
    try:
        store.initialize(cron="0 9 * * *", timezone="UTC", now=datetime(2026, 8, 26, tzinfo=UTC))
        missed = store.reconcile_startup(
            cron="0 9 * * *", timezone="UTC", now=datetime(2026, 8, 28, 12, tzinfo=UTC)
        )
        assert len(missed) == 3
        assert connection.execute("SELECT count(*) FROM notifications").fetchone() == (0,)
    finally:
        connection.close()


def test_due_report_summarizes_backups_and_errors_once(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)
    notifications = NotificationRepository(connection)
    store = DailyReportStore(connection, notifications)
    start = datetime(2026, 8, 28, tzinfo=UTC)
    due = datetime(2026, 8, 28, 9, tzinfo=UTC)
    try:
        operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
        operations.enqueue(
            deduplication_key="backup:one",
            job_id="data",
            kind="backup",
            trigger_source="scheduled",
            queued_at=start,
        )
        run = operations.claim_next(started_at=start + timedelta(hours=1))
        assert run is not None
        operations.finish_run(
            run.run_id,
            result=RunResult.FAILED,
            exit_code=1,
            disk_offline_confirmed=True,
            finished_at=start + timedelta(hours=2),
        )
        store.initialize(cron="0 9 * * *", timezone="UTC", now=start)
        result = store.poll(
            cron="0 9 * * *",
            timezone="UTC",
            health="warning",
            now=due + timedelta(seconds=4),
            poll_seconds=5,
        )
        assert result.formed
        payload = json.loads(
            str(connection.execute("SELECT payload_json FROM notifications").fetchone()[0])
        )
        assert payload["backups"] == ["Data: failed, 3600s"]
        assert payload["errors"] == ["Data backup: failed"]
        assert not store.poll(
            cron="0 9 * * *",
            timezone="UTC",
            health="warning",
            now=due + timedelta(seconds=5),
            poll_seconds=5,
        ).formed
    finally:
        connection.close()
