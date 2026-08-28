from datetime import UTC, datetime
from pathlib import Path

from backup_system.manager.database import open_manager_database
from backup_system.manager.operations import OperationsRepository
from backup_system.manager.scheduler_events import SchedulerEventRepository


def test_scheduler_event_is_persisted_and_deduplicated(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)
    events = SchedulerEventRepository(connection)
    now = datetime(2026, 8, 28, tzinfo=UTC)
    try:
        operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
        with connection:
            first_id, created = events.append_in_transaction(
                deduplication_key="missed:data:one",
                event_type="schedule_missed",
                job_id="data",
                operation_kind="backup",
                scheduled_at=now,
                created_at=now,
            )
            duplicate_id, duplicate_created = events.append_in_transaction(
                deduplication_key="missed:data:one",
                event_type="schedule_missed",
                job_id="data",
                operation_kind="backup",
                scheduled_at=now,
                created_at=now,
            )
        assert created and not duplicate_created and first_id == duplicate_id
        assert connection.execute("SELECT count(*) FROM scheduler_events").fetchone() == (1,)
    finally:
        connection.close()
