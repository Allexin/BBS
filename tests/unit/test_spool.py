from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from backup_system.common.commands import RunCommand, publish_command
from backup_system.manager.layout import RuntimeLayout, initialize_data_layout
from backup_system.manager.spool import CommandSpool


def _layout(root: Path) -> RuntimeLayout:
    (root / "backup-system.root").touch()
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    return layout


def _command(*, created_at: datetime | None = None) -> RunCommand:
    return RunCommand(
        command_id=uuid4(),
        created_at=created_at or datetime.now(UTC),
        kind="run",
        job_id="data",
        operation="backup",
    )


def test_command_moves_through_durable_spool_lifecycle(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    command = _command()
    incoming = publish_command(layout.root, command)
    assert incoming.is_file()
    assert not list(layout.temp.iterdir())

    spool = CommandSpool(layout)
    assert spool.accept_incoming() == (command,)
    assert spool.load_accepted() == (command,)
    completed = spool.mark_completed(command.command_id)
    assert completed == layout.commands_completed / f"{command.command_id}.json"
    assert completed.is_file()


def test_invalid_old_and_duplicate_commands_are_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    malformed = layout.commands_incoming / "not-a-command.json"
    malformed.write_text("{broken", encoding="utf-8")
    old = _command(created_at=datetime.now(UTC) - timedelta(days=2))
    publish_command(layout.root, old)
    spool = CommandSpool(layout)
    assert spool.accept_incoming() == ()
    assert len(list(layout.commands_rejected.iterdir())) == 2

    current = _command()
    publish_command(layout.root, current)
    assert spool.accept_incoming() == (current,)
    spool.mark_completed(current.command_id)
    duplicate = layout.commands_incoming / f"{current.command_id}.json"
    duplicate.write_text(current.model_dump_json(), encoding="utf-8")
    assert spool.accept_incoming() == ()
    assert any("duplicate" in path.name for path in layout.commands_rejected.iterdir())
