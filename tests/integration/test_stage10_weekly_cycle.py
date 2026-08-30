from datetime import UTC, datetime, timedelta
from pathlib import Path

from backup_system.common.config import ScheduleConfig
from backup_system.manager.database import open_manager_database
from backup_system.manager.operations import OperationsRepository, RunResult
from backup_system.manager.schedule_store import ScheduleStore


def _accelerated_week() -> ScheduleConfig:
    return ScheduleConfig.model_validate(
        {
            "cron": "* * * * *",
            "timezone": "UTC",
            "deadline": "08:00",
            "cycle": [
                {"operation": "backup"},
                {"operation": "backup"},
                {"operation": "backup"},
                {"operation": "backup"},
                {"operation": "check", "mode": "subset"},
            ],
        }
    )


def test_accelerated_week_preserves_fifo_and_cycle_state_across_failure(
    tmp_path: Path,
) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)
    operations.upsert_job(
        job_id="stage10", display_name="Stage 10", enabled=True, config_valid=True
    )
    schedule = _accelerated_week()
    store = ScheduleStore(connection, operations)
    first_fire = datetime(2026, 8, 31, tzinfo=UTC)
    store.initialize_new_job("stage10", schedule, now=first_fire - timedelta(seconds=1))

    completed: list[tuple[str, str | None, str]] = []
    try:
        for slot in range(5):
            fire = first_fire + timedelta(minutes=slot)
            poll = store.poll("stage10", schedule, now=fire, poll_seconds=5)
            assert poll.evaluation.due_at == fire
            assert poll.enqueue_result is not None

            claimed = operations.claim_next(started_at=fire + timedelta(seconds=1))
            assert claimed is not None
            assert (claimed.kind, claimed.mode) == (
                schedule.cycle[slot].operation,
                schedule.cycle[slot].mode,
            )

            # A failed phase must be retried; it cannot silently advance the cursor.
            if slot == 2:
                operations.finish_run(
                    claimed.run_id,
                    result=RunResult.FAILED,
                    exit_code=20,
                    disk_offline_confirmed=True,
                    finished_at=fire + timedelta(seconds=2),
                )
                cursor = store.record_completion(
                    "stage10",
                    schedule,
                    result="failed",
                    trigger_source="scheduled",
                    operation=claimed.kind,
                    check_mode=claimed.mode,
                    completed_at=fire + timedelta(seconds=2),
                )
                assert cursor.slot_counter == slot

                operations.enqueue(
                    deduplication_key=f"stage10:retry:{slot}",
                    job_id="stage10",
                    kind=claimed.kind,
                    mode=claimed.mode,
                    trigger_source="scheduled",
                    scheduled_at=fire,
                    queued_at=fire + timedelta(seconds=3),
                )
                claimed = operations.claim_next(started_at=fire + timedelta(seconds=4))
                assert claimed is not None

            operations.finish_run(
                claimed.run_id,
                result=RunResult.SUCCESS,
                exit_code=0,
                disk_offline_confirmed=True,
                finished_at=fire + timedelta(seconds=5),
            )
            cursor = store.record_completion(
                "stage10",
                schedule,
                result="success",
                trigger_source="scheduled",
                operation=claimed.kind,
                check_mode=claimed.mode,
                completed_at=fire + timedelta(seconds=5),
            )
            assert cursor.slot_counter == slot + 1
            completed.append((claimed.kind, claimed.mode, "success"))

        assert completed == [
            ("backup", None, "success"),
            ("backup", None, "success"),
            ("backup", None, "success"),
            ("backup", None, "success"),
            ("check", "subset", "success"),
        ]
        assert connection.execute(
            "SELECT slot_counter, recovery_check_required FROM schedule_state"
        ).fetchone() == (5, 0)
        assert connection.execute(
            "SELECT count(*) FROM operations WHERE state != 'completed'"
        ).fetchone() == (0,)
    finally:
        connection.close()
