import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from backup_system.ctl.state_reader import LocalStateReader
from backup_system.manager.database import open_manager_database
from backup_system.manager.operations import OperationsRepository
from backup_system.manager.safety import SafetyLatchRepository


def _database(path: Path) -> Path:
    connection = open_manager_database(path)
    repository = OperationsRepository(connection)
    repository.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    repository.enqueue(
        deduplication_key="manual:backup",
        job_id="data",
        kind="backup",
        trigger_source="manual",
    )
    repository.enqueue(
        deduplication_key="manual:check",
        job_id="data",
        kind="check",
        trigger_source="manual",
    )
    repository.claim_next()
    connection.close()
    return path


def test_reader_projects_status_jobs_and_queue(tmp_path: Path) -> None:
    path = _database(tmp_path / "manager.sqlite3")
    with LocalStateReader(path) as reader:
        assert reader.status()["queued_count"] == 1
        assert reader.status()["active"]["kind"] == "backup"
        assert reader.jobs()["jobs"][0]["enabled"] is True
        queue = reader.queue()["operations"]
        assert [item["position"] for item in queue] == [0, 1]


def test_reader_connection_is_query_only(tmp_path: Path) -> None:
    path = _database(tmp_path / "manager.sqlite3")
    with LocalStateReader(path) as reader, pytest.raises(sqlite3.OperationalError):
        reader._connection.execute("DELETE FROM jobs")


def test_reader_projects_active_disk_latch_without_private_identity(tmp_path: Path) -> None:
    path = _database(tmp_path / "manager.sqlite3")
    connection = open_manager_database(path)
    try:
        with connection:
            SafetyLatchRepository(connection).set_disk_lifecycle_in_transaction(
                job_id="data",
                source_run_id=UUID("00000000-0000-4000-8000-000000000001"),
                reason="offline_not_confirmed",
                created_at=datetime.now(UTC),
            )
    finally:
        connection.close()
    with LocalStateReader(path) as reader:
        latch = reader.disk_latch("data")
    assert latch is not None
    assert latch["reason"] == "offline_not_confirmed"
    assert "serial" not in json.dumps(latch)
