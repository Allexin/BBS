"""Strict executor-owned restore request contract."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backup_system.common.config import validate_job_id
from backup_system.common.ids import parse_uuid4


class RestoreRequestError(ValueError):
    pass


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    request_id: UUID
    job_id: str
    version: str
    path: str
    target: str

    _job_id = field_validator("job_id")(validate_job_id)

    @field_validator("request_id")
    @classmethod
    def uuid4_only(cls, value: UUID) -> UUID:
        return parse_uuid4(str(value))

    @model_validator(mode="after")
    def validate_paths(self) -> RestoreRequest:
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
        return self


def load_restore_request(path: Path, *, expected_job_id: str) -> RestoreRequest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        request = RestoreRequest.model_validate(payload)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RestoreRequestError("restore request is invalid") from error
    if request.job_id != expected_job_id:
        raise RestoreRequestError("restore request job does not match executor job")
    return request
