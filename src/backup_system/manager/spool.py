"""Atomic local command publication and durable spool lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from backup_system.common.commands import LOCAL_COMMAND_ADAPTER, MAX_COMMAND_BYTES, LocalCommand
from backup_system.common.ids import parse_uuid4
from backup_system.common.time import require_aware, utc_now
from backup_system.manager.layout import RuntimeLayout

MAX_INCOMING_AGE = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)


class SpoolValidationError(ValueError):
    """An incoming command artifact violates the spool contract."""


class CommandSpool:
    def __init__(self, layout: RuntimeLayout) -> None:
        self._layout = layout

    def accept_incoming(self, *, now: datetime | None = None) -> tuple[LocalCommand, ...]:
        observed_at = require_aware(now or utc_now())
        accepted: list[LocalCommand] = []
        for path in sorted(self._layout.commands_incoming.iterdir(), key=lambda item: item.name):
            try:
                command = self._read_command(path)
                expected_name = f"{command.command_id}.json"
                if path.name != expected_name:
                    raise SpoolValidationError("filename does not match command_id")
                age = observed_at - command.created_at
                if age > MAX_INCOMING_AGE:
                    raise SpoolValidationError("command is too old")
                if age < -MAX_FUTURE_SKEW:
                    raise SpoolValidationError("command timestamp is too far in the future")
                destination = self._layout.commands_accepted / expected_name
                if self._artifact_exists(expected_name):
                    raise SpoolValidationError("command_id was already delivered")
                path.rename(destination)
                accepted.append(command)
            except (OSError, SpoolValidationError, ValidationError, ValueError):
                self._reject(path)
        return tuple(accepted)

    def load_accepted(self) -> tuple[LocalCommand, ...]:
        commands: list[LocalCommand] = []
        for path in sorted(self._layout.commands_accepted.glob("*.json")):
            try:
                command = self._read_command(path)
                if path.name != f"{command.command_id}.json":
                    raise SpoolValidationError("filename does not match command_id")
                commands.append(command)
            except (OSError, SpoolValidationError, ValidationError, ValueError):
                self._reject(path)
        return tuple(commands)

    def mark_completed(self, command_id: UUID) -> Path:
        command_id = parse_uuid4(str(command_id))
        source = self._layout.commands_accepted / f"{command_id}.json"
        destination = self._layout.commands_completed / source.name
        source.rename(destination)
        return destination

    def mark_rejected(self, command_id: UUID) -> Path:
        command_id = parse_uuid4(str(command_id))
        source = self._layout.commands_accepted / f"{command_id}.json"
        destination = self._layout.commands_rejected / source.name
        if destination.exists():
            destination = self._layout.commands_rejected / (
                f"{source.stem}.{uuid4()}.duplicate{source.suffix}"
            )
        source.rename(destination)
        return destination

    def _read_command(self, path: Path) -> LocalCommand:
        if path.is_symlink() or not path.is_file():
            raise SpoolValidationError("command artifact must be a regular file")
        size = path.stat().st_size
        if size <= 0 or size > MAX_COMMAND_BYTES:
            raise SpoolValidationError("invalid command file size")
        try:
            payload = path.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise SpoolValidationError("command is not UTF-8") from error
        return LOCAL_COMMAND_ADAPTER.validate_json(payload)

    def _artifact_exists(self, name: str) -> bool:
        return any(
            (directory / name).exists()
            for directory in (
                self._layout.commands_accepted,
                self._layout.commands_completed,
                self._layout.commands_rejected,
            )
        )

    def _reject(self, source: Path) -> Path:
        if not source.exists():
            return source
        destination = self._layout.commands_rejected / source.name
        if destination.exists() or self._artifact_exists(source.name):
            destination = self._layout.commands_rejected / (
                f"{source.stem}.{uuid4()}.duplicate{source.suffix}"
            )
        source.rename(destination)
        return destination
