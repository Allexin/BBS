import sqlite3
from pathlib import Path

import pytest

from backup_system.ctl.state_reader import LocalStateReader
from backup_system.manager.database import open_manager_database
from backup_system.manager.operations import OperationsRepository


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
