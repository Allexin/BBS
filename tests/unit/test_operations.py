import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from backup_system.manager.database import open_manager_database
from backup_system.manager.operations import (
    EnqueueDisposition,
    OperationsRepository,
    OperationState,
    RemoveDisposition,
)


def _repository(path: Path) -> tuple[OperationsRepository, sqlite3.Connection]:
    connection = open_manager_database(path)
    repository = OperationsRepository(connection)
    repository.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    return repository, connection


def test_enqueue_is_idempotent_and_coalesces_unfinished_work(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path / "manager.sqlite3")
    try:
        first = repository.enqueue(
            deduplication_key="manual:command-1",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        duplicate = repository.enqueue(
            deduplication_key="manual:command-1",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        overlap = repository.enqueue(
            deduplication_key="manual:command-2",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        assert first.disposition is EnqueueDisposition.CREATED
        assert duplicate == type(duplicate)(first.operation_id, EnqueueDisposition.DEDUPLICATED)
        assert overlap == type(overlap)(first.operation_id, EnqueueDisposition.COALESCED)
    finally:
        connection.close()


def test_remove_changes_only_queued_operation_and_allows_new_work(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path / "manager.sqlite3")
    try:
        first = repository.enqueue(
            deduplication_key="manual:command-1",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        assert (
            repository.remove_queued(first.operation_id, removed_at=datetime.now(UTC))
            is RemoveDisposition.REMOVED
        )
        assert repository.remove_queued(first.operation_id) is RemoveDisposition.NOT_QUEUED
        assert repository.remove_queued(uuid4()) is RemoveDisposition.NOT_FOUND
        second = repository.enqueue(
            deduplication_key="manual:command-2",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        assert second.disposition is EnqueueDisposition.CREATED
        row = connection.execute(
            "SELECT state, terminal_reason FROM operations WHERE operation_id = ?",
            (str(first.operation_id),),
        ).fetchone()
        assert row == (OperationState.REMOVED, "manual_queue_remove")
    finally:
        connection.close()
