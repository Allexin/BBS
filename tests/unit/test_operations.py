import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from backup_system.manager.database import open_manager_database
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import (
    EnqueueDisposition,
    OperationsRepository,
    OperationState,
    RemoveDisposition,
    RunResult,
    StateTransitionError,
)
from backup_system.manager.safety import SafetyLatchRepository


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
        assert overlap == type(overlap)(
            first.operation_id,
            EnqueueDisposition.COALESCED,
            OperationState.QUEUED,
        )
    finally:
        connection.close()


def test_failed_run_and_unconfirmed_offline_queue_immediate_alerts(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    notifications = NotificationRepository(connection)
    repository = OperationsRepository(connection, notifications)
    repository.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    try:
        repository.enqueue(
            deduplication_key="manual:failed",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        run = repository.claim_next()
        assert run is not None
        repository.finish_run(
            run.run_id,
            result=RunResult.FAILED,
            exit_code=10,
            disk_offline_confirmed=False,
        )
        assert connection.execute("SELECT kind FROM notifications ORDER BY rowid").fetchall() == [
            ("run_failed",),
            ("disk_offline_unconfirmed",),
        ]
        latch = SafetyLatchRepository(connection).active()
        assert latch is not None
        assert latch.job_id == "data" and latch.source_run_id == run.run_id

        repository.enqueue(
            deduplication_key="manual:blocked-after-offline-failure",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        assert repository.claim_next() is None
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


def test_restart_reconciles_running_and_discards_queued_tail(tmp_path: Path) -> None:
    path = tmp_path / "manager.sqlite3"
    repository, connection = _repository(path)
    active = repository.enqueue(
        deduplication_key="manual:active",
        job_id="data",
        kind="backup",
        trigger_source="manual",
    )
    queued = repository.enqueue(
        deduplication_key="manual:queued",
        job_id="data",
        kind="check",
        trigger_source="manual",
    )
    claimed = repository.claim_next()
    assert claimed is not None and claimed.operation_id == active.operation_id
    connection.close()

    connection = open_manager_database(path)
    try:
        restarted = OperationsRepository(connection)
        result = restarted.reconcile_startup()
        assert result.interrupted_run_ids == (claimed.run_id,)
        assert result.discarded_operation_ids == (queued.operation_id,)
        states = dict(connection.execute("SELECT operation_id, state FROM operations").fetchall())
        assert states[str(active.operation_id)] == OperationState.COMPLETED
        assert states[str(queued.operation_id)] == OperationState.DISCARDED_ON_RESTART
        run = connection.execute(
            "SELECT state, result FROM runs WHERE run_id = ?", (str(claimed.run_id),)
        ).fetchone()
        assert run == ("finished", RunResult.INTERRUPTED)
        latch = SafetyLatchRepository(connection).active()
        assert latch is not None
        assert latch.job_id == "data" and latch.source_run_id == claimed.run_id
        repeated = restarted.reconcile_startup()
        assert repeated.interrupted_run_ids == ()
        assert repeated.discarded_operation_ids == ()
    finally:
        connection.close()


def test_safety_latch_allows_only_successful_manual_recover(tmp_path: Path) -> None:
    path = tmp_path / "manager.sqlite3"
    repository, connection = _repository(path)
    repository.enqueue(
        deduplication_key="run:unsafe",
        job_id="data",
        kind="backup",
        trigger_source="scheduled",
    )
    unsafe_run = repository.claim_next()
    assert unsafe_run is not None
    connection.close()

    connection = open_manager_database(path)
    repository = OperationsRepository(connection)
    try:
        repository.reconcile_startup()
        blocked = repository.enqueue(
            deduplication_key="run:blocked",
            job_id="data",
            kind="backup",
            trigger_source="scheduled",
        )
        assert repository.claim_next() is None

        repository.enqueue(
            deduplication_key="recover:first",
            job_id="data",
            kind="recover",
            trigger_source="manual",
        )
        failed_recover = repository.claim_next()
        assert failed_recover is not None and failed_recover.kind == "recover"
        repository.finish_run(
            failed_recover.run_id,
            result=RunResult.FAILED,
            exit_code=1,
            disk_offline_confirmed=True,
        )
        assert SafetyLatchRepository(connection).active() is not None
        assert repository.claim_next() is None

        repository.enqueue(
            deduplication_key="recover:second",
            job_id="data",
            kind="recover",
            trigger_source="manual",
        )
        successful_recover = repository.claim_next()
        assert successful_recover is not None and successful_recover.kind == "recover"
        repository.finish_run(
            successful_recover.run_id,
            result=RunResult.SUCCESS,
            exit_code=0,
            disk_offline_confirmed=True,
        )
        assert SafetyLatchRepository(connection).active() is None
        resumed = repository.claim_next()
        assert resumed is not None and resumed.operation_id == blocked.operation_id
    finally:
        connection.close()


def test_service_stop_discards_only_queued_tail(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path / "manager.sqlite3")
    try:
        active = repository.enqueue(
            deduplication_key="manual:active",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        queued = repository.enqueue(
            deduplication_key="manual:queued",
            job_id="data",
            kind="check",
            trigger_source="manual",
        )
        claimed = repository.claim_next()
        assert claimed is not None and claimed.operation_id == active.operation_id
        assert repository.discard_queued_for_service_stop() == (queued.operation_id,)
        assert repository.discard_queued_for_service_stop() == ()
        states = dict(connection.execute("SELECT operation_id, state FROM operations"))
        assert states[str(active.operation_id)] == OperationState.RUNNING
        assert states[str(queued.operation_id)] == OperationState.DISCARDED_ON_SERVICE_STOP
    finally:
        connection.close()
