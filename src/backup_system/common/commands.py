"""Versioned local command spool contracts."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from backup_system.common.config import validate_job_id
from backup_system.common.ids import parse_uuid4
from backup_system.common.time import require_aware

MAX_COMMAND_BYTES = 64 * 1024


class CommandBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    created_at: datetime
    kind: str

    @field_validator("command_id")
    @classmethod
    def uuid4_only(cls, value: UUID) -> UUID:
        return parse_uuid4(str(value))

    @field_validator("created_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        value = require_aware(value)
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("created_at must be UTC")
        return value


class RunCommand(CommandBase):
    kind: Literal["run"]
    job_id: str
    operation: Literal["check", "restore", "restore-test", "repair-mirror", "recover"] | None = None
    mode: Literal["subset", "full"] | None = None
    version: str | None = None
    path: str | None = None
    target: str | None = None

    _job_id = field_validator("job_id")(validate_job_id)

    @model_validator(mode="after")
    def operation_fields(self) -> RunCommand:
        if (self.operation == "check") != (self.mode is not None):
            raise ValueError("mode is required only for check")
        restore_values = (self.version, self.path, self.target)
        if self.operation == "restore":
            if any(value is None for value in restore_values):
                raise ValueError("restore requires version, path and target")
            assert self.path is not None and self.target is not None
            if not self.version:
                raise ValueError("restore version must not be empty")
            source = PureWindowsPath(self.path)
            if self.path != "." and (
                not self.path
                or source.is_absolute()
                or source.drive
                or source.root
                or ".." in source.parts
            ):
                raise ValueError("restore path must be relative or '.'")
            if any(character in self.path for character in "*?"):
                raise ValueError("restore path cannot contain wildcards")
            target = PureWindowsPath(self.target)
            if not target.is_absolute() or not target.drive:
                raise ValueError("restore target must be an absolute Windows path")
        elif any(value is not None for value in restore_values):
            raise ValueError("version, path and target are allowed only for restore")
        return self


class CancelCurrentCommand(CommandBase):
    kind: Literal["cancel-current"]


class QueueRemoveCommand(CommandBase):
    kind: Literal["queue-remove"]
    operation_id: UUID

    @field_validator("operation_id")
    @classmethod
    def operation_uuid4_only(cls, value: UUID) -> UUID:
        return parse_uuid4(str(value))


LocalCommand = Annotated[
    RunCommand | CancelCurrentCommand | QueueRemoveCommand,
    Field(discriminator="kind"),
]
LOCAL_COMMAND_ADAPTER: TypeAdapter[LocalCommand] = TypeAdapter(LocalCommand)


def publish_command(root: Path, command: CommandBase) -> Path:
    """Flush a command to same-volume temp, then publish without overwriting input."""
    payload = command.model_dump_json().encode("utf-8")
    if len(payload) > MAX_COMMAND_BYTES:
        raise ValueError("command exceeds maximum size")
    temporary = root / "data" / "temp" / f"command-{command.command_id}-{uuid4()}.tmp"
    destination = root / "data" / "commands" / "incoming" / f"{command.command_id}.json"
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.rename(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination
