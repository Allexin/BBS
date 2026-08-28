import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from backup_system.common.commands import CancelCurrentCommand, QueueRemoveCommand, RunCommand
from backup_system.manager.command_processor import CommandDisposition, CommandProcessor
from backup_system.manager.database import open_manager_database
from backup_system.manager.layout import RuntimeLayout, initialize_data_layout
from backup_system.manager.operations import OperationsRepository
from backup_system.manager.spool import CommandSpool, publish_command


def _runtime(
    root: Path,
) -> tuple[RuntimeLayout, OperationsRepository, sqlite3.Connection]:
    (root / "backup-system.root").touch()
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    operations = OperationsRepository(connection)
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    return layout, operations, connection


def test_run_command_is_enqueued_and_completed(tmp_path: Path) -> None:
    layout, operations, connection = _runtime(tmp_path)
    try:
        command = RunCommand(
            command_id=uuid4(),
            created_at=datetime.now(UTC),
            kind="run",
            job_id="data",
            operation="backup",
        )
        publish_command(layout, command)
        spool = CommandSpool(layout)
        assert spool.accept_incoming() == (command,)
        outcomes = CommandProcessor(
            spool, operations, cancel_current=lambda: None
        ).process_accepted()
        assert outcomes[0].disposition is CommandDisposition.ENQUEUED
        assert (layout.commands_completed / f"{command.command_id}.json").is_file()
        row = connection.execute("SELECT deduplication_key, state FROM operations").fetchone()
        assert row == (f"command:{command.command_id}", "queued")
    finally:
        connection.close()


def test_replayed_accepted_command_is_deduplicated(tmp_path: Path) -> None:
    layout, operations, connection = _runtime(tmp_path)
    try:
        command = RunCommand(
            command_id=uuid4(),
            created_at=datetime.now(UTC),
            kind="run",
            job_id="data",
            operation="backup",
        )
        publish_command(layout, command)
        spool = CommandSpool(layout)
        spool.accept_incoming()
        existing = operations.enqueue(
            deduplication_key=f"command:{command.command_id}",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        outcome = CommandProcessor(
            spool, operations, cancel_current=lambda: None
        ).process_accepted()[0]
        assert outcome.disposition is CommandDisposition.DEDUPLICATED
        assert outcome.operation_id == existing.operation_id
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone() == (1,)
    finally:
        connection.close()


def test_remove_and_cancel_commands_dispatch_to_typed_handlers(tmp_path: Path) -> None:
    layout, operations, connection = _runtime(tmp_path)
    try:
        queued = operations.enqueue(
            deduplication_key="manual:queued",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        remove = QueueRemoveCommand(
            command_id=uuid4(),
            created_at=datetime.now(UTC),
            kind="queue-remove",
            operation_id=queued.operation_id,
        )
        cancelled: list[bool] = []
        cancel = CancelCurrentCommand(
            command_id=uuid4(), created_at=datetime.now(UTC), kind="cancel-current"
        )
        publish_command(layout, remove)
        publish_command(layout, cancel)
        spool = CommandSpool(layout)
        spool.accept_incoming()
        outcomes = CommandProcessor(
            spool, operations, cancel_current=lambda: cancelled.append(True)
        ).process_accepted()
        assert {outcome.disposition for outcome in outcomes} == {
            CommandDisposition.REMOVED,
            CommandDisposition.CANCEL_REQUESTED,
        }
        assert cancelled == [True]
    finally:
        connection.close()
