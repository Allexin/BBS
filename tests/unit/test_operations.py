import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from backup_system.manager.database import open_manager_database
from backup_system.manager.operations import (
    EnqueueDisposition,
    OperationsRepository,
    OperationState,
    RemoveDisposition,
    RunResult,
    StateTransitionError,
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


def test_claim_progress_and_finish_are_atomic_state_transitions(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path / "manager.sqlite3")
    try:
        operation = repository.enqueue(
            deduplication_key="manual:command-1",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        claimed = repository.claim_next()
        assert claimed is not None
        assert claimed.operation_id == operation.operation_id
        assert repository.claim_next() is None

        repository.update_stage(claimed.run_id, "backing_up")
        repository.update_progress(
            claimed.run_id, files_done=2, files_total=3, bytes_done=20, bytes_total=30
        )
        repository.finish_run(
            claimed.run_id,
            result=RunResult.SUCCESS,
            exit_code=0,
            disk_offline_confirmed=True,
            snapshot_id="snapshot-1",
            bytes_added=20,
        )

        operation_row = connection.execute(
            "SELECT state FROM operations WHERE operation_id = ?",
            (str(operation.operation_id),),
        ).fetchone()
        run_row = connection.execute(
            """SELECT state, result, files_done, bytes_done, disk_offline_confirmed
            FROM runs WHERE run_id = ?""",
            (str(claimed.run_id),),
        ).fetchone()
        event_types = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM run_events WHERE run_id = ? ORDER BY event_seq",
                (str(claimed.run_id),),
            )
        ]
        assert operation_row == (OperationState.COMPLETED,)
        assert run_row == ("finished", RunResult.SUCCESS, 2, 20, 1)
        assert event_types == ["run_started", "stage_changed", "run_finished"]
    finally:
        connection.close()


def test_finished_run_rejects_further_mutation(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path / "manager.sqlite3")
    try:
        repository.enqueue(
            deduplication_key="manual:command-1",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        claimed = repository.claim_next()
        assert claimed is not None
        repository.finish_run(
            claimed.run_id,
            result=RunResult.FAILED,
            exit_code=25,
            disk_offline_confirmed=True,
        )
        with pytest.raises(StateTransitionError):
            repository.update_progress(claimed.run_id, bytes_done=1)
        with pytest.raises(StateTransitionError):
            repository.finish_run(
                claimed.run_id,
                result=RunResult.SUCCESS,
                exit_code=0,
                disk_offline_confirmed=True,
            )
    finally:
        connection.close()
