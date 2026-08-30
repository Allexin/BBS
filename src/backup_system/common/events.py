"""Versioned executor event contracts."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from backup_system.common.smart import SmartMetrics
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


class RestoreVersionResolved(EventBase):
    event: Literal["restore_version_resolved"]
    version: str = Field(pattern=r"^(?:latest|[0-9a-f]{64})$")


class RestoreTargetReady(EventBase):
    event: Literal["restore_target_ready"]
    result_path: str


class RestoreCompleted(EventBase):
    event: Literal["restore_completed"]
    result_path: str
    files_restored: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)


class DiskOfflineConfirmed(EventBase):
    event: Literal["disk_offline_confirmed"]


class DiskOfflineFailed(EventBase):
    event: Literal["disk_offline_failed"]


class SmartObserved(EventBase):
    event: Literal["smart_observed"]
    disk_id: str
    collection_success: bool
    health: Literal["healthy", "warning", "critical", "unknown"]
    identity_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metrics: SmartMetrics
    reason: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    media_type: Literal["hdd", "ssd", "nvme", "unknown"] = "unknown"
    bus_type: str | None = None
    capacity_bytes: int | None = Field(default=None, ge=0)
    mount_points: tuple[str, ...] = ()


class SmartTestDiskFinished(EventBase):
    event: Literal["smart_test_disk_finished"]
    disk_id: str
    identity_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_type: Literal["short", "long"]
    result: Literal["success", "failed", "timeout", "unsupported"]
    reason: str
    duration_seconds: int = Field(ge=0)
    remaining_percent: int | None = Field(default=None, ge=0, le=100)


class RunFinished(EventBase):
    event: Literal["run_finished"]
    result: Literal["success", "warning", "failed", "cancelled", "interrupted"]
    exit_code: int | None = Field(default=None, ge=0)
    disk_offline_confirmed: bool | None = None


KnownExecutorEvent = Annotated[
    RunStarted
    | StageChanged
    | Progress
    | SnapshotCreated
    | RestoreVersionResolved
    | RestoreTargetReady
    | RestoreCompleted
    | DiskOfflineConfirmed
    | DiskOfflineFailed
    | SmartObserved
    | SmartTestDiskFinished
    | RunFinished,
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
    known_names = {
        "run_started",
        "stage_changed",
        "progress",
        "snapshot_created",
        "restore_version_resolved",
        "restore_target_ready",
        "restore_completed",
        "disk_offline_confirmed",
        "disk_offline_failed",
        "smart_observed",
        "smart_test_disk_finished",
        "run_finished",
    }
    if value.get("event") not in known_names:
        return UnknownExecutorEvent.model_validate(value)
    return _EVENT_ADAPTER.validate_python(value)
