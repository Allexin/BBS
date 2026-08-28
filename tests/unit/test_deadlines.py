from datetime import UTC, datetime, timedelta
from pathlib import Path

from backup_system.common.config import ScheduleConfig
from backup_system.manager.database import open_manager_database
from backup_system.manager.deadlines import DeadlineMonitor, deadline_for
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import OperationsRepository, RunResult


def _schedule(*, deadline: str | None = "08:00") -> ScheduleConfig:
    return ScheduleConfig.model_validate(
        {
            "cron": "0 22 * * *",
            "timezone": "Europe/Samara",
            "deadline": deadline,
            "cycle": [{"operation": "backup"}],
        }
    )


def test_deadline_is_first_configured_local_time_after_schedule() -> None:
    scheduled = datetime(2026, 8, 28, 18, tzinfo=UTC)  # 22:00 Samara
    assert deadline_for(_schedule(), scheduled) == datetime(2026, 8, 29, 4, tzinfo=UTC)
    assert deadline_for(_schedule(deadline=None), scheduled) is None


def test_nonexistent_dst_deadline_moves_to_first_valid_minute() -> None:
    schedule = ScheduleConfig.model_validate(
        {
            "cron": "0 0 * * *",
            "timezone": "Europe/Berlin",
            "deadline": "02:30",
            "cycle": [{"operation": "backup"}],
        }
    )
    scheduled = datetime(2026, 3, 28, 23, tzinfo=UTC)
    assert deadline_for(schedule, scheduled) == datetime(2026, 3, 29, 1, tzinfo=UTC)


def test_monitor_persists_initial_and_final_overrun_without_changing_result(
    tmp_path: Path,
) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)
    notifications = NotificationRepository(connection)
    monitor = DeadlineMonitor(connection, notifications)
    started = datetime(2026, 8, 28, tzinfo=UTC)
    deadline = started + timedelta(hours=1)
    try:
        operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
        operations.enqueue(
            deduplication_key="scheduled:one",
            job_id="data",
            kind="backup",
            trigger_source="scheduled",
            scheduled_at=started,
            queued_at=started,
        )
        run = operations.claim_next(started_at=started)
        assert run is not None
        operations.update_stage(run.run_id, "backup", changed_at=started)
        monitor.assign(run.run_id, deadline)

        sweep = monitor.sweep(now=deadline + timedelta(minutes=10))
        assert sweep.active_alerts == 1 and sweep.completion_summaries == 0
        assert monitor.sweep(now=deadline + timedelta(minutes=20)).active_alerts == 0

        finished = deadline + timedelta(minutes=30)
        operations.finish_run(
            run.run_id,
            result=RunResult.SUCCESS,
            exit_code=0,
            disk_offline_confirmed=True,
            finished_at=finished,
        )
        sweep = monitor.sweep(now=finished)
        assert sweep.completion_summaries == 1
        assert connection.execute(
            """SELECT result, deadline_overrun_seconds, deadline_final_notified
            FROM runs WHERE run_id = ?""",
            (str(run.run_id),),
        ).fetchone() == (RunResult.SUCCESS, 1800, 1)
        assert connection.execute(
            "SELECT kind FROM notifications ORDER BY created_at, rowid"
        ).fetchall() == [("deadline_overrun",), ("deadline_overrun_finished",)]
    finally:
        connection.close()
