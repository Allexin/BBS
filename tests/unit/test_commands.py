from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backup_system.common.commands import LOCAL_COMMAND_ADAPTER, QueueRemoveCommand, RunCommand


def test_restore_command_validates_paths_and_discriminator() -> None:
    command = LOCAL_COMMAND_ADAPTER.validate_python(
        {
            "schema_version": 1,
            "command_id": str(uuid4()),
            "created_at": datetime.now(UTC),
            "kind": "run",
            "job_id": "data",
            "operation": "restore",
            "version": "latest",
            "path": r"Photos\2020",
            "target": r"C:\Restore",
        }
    )
    assert isinstance(command, RunCommand)


def test_restore_rejects_parent_traversal() -> None:
    with pytest.raises(ValidationError):
        LOCAL_COMMAND_ADAPTER.validate_python(
            {
                "schema_version": 1,
                "command_id": str(uuid4()),
                "created_at": datetime.now(UTC),
                "kind": "run",
                "job_id": "data",
                "operation": "restore",
                "version": "latest",
                "path": r"..\secret",
                "target": r"C:\Restore",
            }
        )


def test_queue_remove_requires_uuid4() -> None:
    command = QueueRemoveCommand.model_validate(
        {
            "command_id": str(uuid4()),
            "created_at": datetime.now(UTC),
            "kind": "queue-remove",
            "operation_id": str(uuid4()),
        }
    )
    assert command.kind == "queue-remove"
