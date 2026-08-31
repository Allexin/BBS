import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from backup_system.common.commands import (
    CancelCurrentCommand,
    QueueRemoveCommand,
    RunCommand,
    publish_command,
)
from backup_system.manager.command_processor import CommandDisposition, CommandProcessor
from backup_system.manager.database import open_manager_database
from backup_system.manager.layout import RuntimeLayout, initialize_data_layout
from backup_system.manager.operations import OperationsRepository
from backup_system.manager.spool import CommandSpool


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
        )
        publish_command(layout.root, command)
        spool = CommandSpool(layout)
        assert spool.accept_incoming() == (command,)
        outcomes = CommandProcessor(
            spool,
            operations,
            cancel_current=lambda: None,
            default_operations={"data": "backup"},
        ).process_accepted()
        assert outcomes[0].disposition is CommandDisposition.ENQUEUED
        assert (layout.commands_completed / f"{command.command_id}.json").is_file()
        row = connection.execute("SELECT deduplication_key, state FROM operations").fetchone()
        assert row == (f"command:{command.command_id}", "queued")
    finally:
        connection.close()


def test_generic_run_resolves_smart_test_from_job_configuration(tmp_path: Path) -> None:
    layout, operations, connection = _runtime(tmp_path)
    try:
        command = RunCommand(
            command_id=uuid4(),
            created_at=datetime.now(UTC),
            kind="run",
            job_id="data",
        )
        publish_command(layout.root, command)
        spool = CommandSpool(layout)
        spool.accept_incoming()
        outcome = CommandProcessor(
            spool,
            operations,
            cancel_current=lambda: None,
            default_operations={"data": "smart-test"},
        ).process_accepted()[0]
        assert outcome.disposition is CommandDisposition.ENQUEUED
        assert connection.execute("SELECT kind FROM operations").fetchone() == ("smart-test",)
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
        )
        publish_command(layout.root, command)
        spool = CommandSpool(layout)
        spool.accept_incoming()
        existing = operations.enqueue(
            deduplication_key=f"command:{command.command_id}",
            job_id="data",
            kind="backup",
            trigger_source="manual",
        )
        outcome = CommandProcessor(
            spool,
            operations,
            cancel_current=lambda: None,
            default_operations={"data": "backup"},
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
        publish_command(layout.root, remove)
        publish_command(layout.root, cancel)
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
        result = json.loads(
            (layout.commands_completed / f"{remove.command_id}.result.json").read_text(
                encoding="utf-8"
            )
        )
        assert result["disposition"] == "removed"
        assert result["operation_id"] == str(queued.operation_id)
    finally:
        connection.close()


def test_restore_is_queued_for_privileged_fifo_resolution(tmp_path: Path) -> None:
    layout, operations, connection = _runtime(tmp_path)
    try:
        command = RunCommand(
            command_id=uuid4(),
            created_at=datetime.now(UTC),
            kind="run",
            job_id="data",
            operation="restore",
            version="latest",
            path=".",
            target=r"D:\Restores",
        )
        publish_command(layout.root, command)
        spool = CommandSpool(layout)
        spool.accept_incoming()
        CommandProcessor(spool, operations, cancel_current=lambda: None).process_accepted()
        kind, request = connection.execute("SELECT kind, request_json FROM operations").fetchone()
        assert kind == "resolve-restore"
        assert '"request_id"' in request
        assert '"version":"latest"' in request
    finally:
        connection.close()


def test_restore_resolution_does_not_block_following_accepted_command(tmp_path: Path) -> None:
    layout, operations, connection = _runtime(tmp_path)
    try:
        rejected = RunCommand(
            command_id=UUID("00000000-0000-4000-8000-000000000001"),
            created_at=datetime.now(UTC),
            kind="run",
            job_id="data",
            operation="restore",
            version="latest",
            path=".",
            target=r"D:\Restores",
        )
        accepted = RunCommand(
            command_id=UUID("10000000-0000-4000-8000-000000000001"),
            created_at=datetime.now(UTC),
            kind="run",
            job_id="data",
        )
        publish_command(layout.root, rejected)
        publish_command(layout.root, accepted)
        spool = CommandSpool(layout)
        spool.accept_incoming()

        outcomes = CommandProcessor(
            spool,
            operations,
            cancel_current=lambda: None,
            default_operations={"data": "backup"},
        ).process_accepted()
        assert [item.disposition for item in outcomes] == [
            CommandDisposition.ENQUEUED,
            CommandDisposition.ENQUEUED,
        ]
        assert (layout.commands_completed / f"{rejected.command_id}.json").is_file()
        assert (layout.commands_completed / f"{accepted.command_id}.json").is_file()
    finally:
        connection.close()
