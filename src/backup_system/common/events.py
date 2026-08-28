"""Versioned executor event contracts."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from backup_system.common.time import require_aware


class EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event: str
    timestamp: datetime

    _aware_timestamp = field_validator("timestamp")(require_aware)


class RunStarted(EventBase):
    event: Literal["run_started"]
    run_id: UUID
    job_id: str


class StageChanged(EventBase):
    event: Literal["stage_changed"]
    stage: str


class Progress(EventBase):
    event: Literal["progress"]
    stage: str
    files_done: int | None = Field(default=None, ge=0)
    files_total: int | None = Field(default=None, ge=0)
    bytes_done: int | None = Field(default=None, ge=0)
    bytes_total: int | None = Field(default=None, ge=0)


class SnapshotCreated(EventBase):
    event: Literal["snapshot_created"]
    snapshot_id: str
    bytes_added: int = Field(ge=0)


class RunFinished(EventBase):
    event: Literal["run_finished"]
    result: Literal["success", "warning", "failed", "cancelled", "interrupted"]


KnownExecutorEvent = Annotated[
    RunStarted | StageChanged | Progress | SnapshotCreated | RunFinished,
    Field(discriminator="event"),
]
_EVENT_ADAPTER: TypeAdapter[KnownExecutorEvent] = TypeAdapter(KnownExecutorEvent)


class UnknownExecutorEvent(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: int
    event: str
    timestamp: datetime

    _aware_timestamp = field_validator("timestamp")(require_aware)


def parse_executor_event(value: dict[str, Any]) -> KnownExecutorEvent | UnknownExecutorEvent:
    known_names = {"run_started", "stage_changed", "progress", "snapshot_created", "run_finished"}
    if value.get("event") not in known_names:
        return UnknownExecutorEvent.model_validate(value)
    return _EVENT_ADAPTER.validate_python(value)
