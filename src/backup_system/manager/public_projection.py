"""Strict sanitized contracts and atomic publication for the static Status UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from backup_system.common.time import require_aware


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicRun(PublicModel):
    state: Literal["queued", "running", "finished"]
    result: Literal["success", "warning", "failed", "cancelled", "interrupted"] | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    deadline_exceeded: bool = False
    stage: str | None = None


class PublicBackupMetrics(PublicModel):
    source_logical_bytes: int | None = Field(default=None, ge=0)
    protected_logical_bytes: int | None = Field(default=None, ge=0)
    retained_logical_bytes: int | None = Field(default=None, ge=0)
    bytes_read: int | None = Field(default=None, ge=0)
    bytes_written: int | None = Field(default=None, ge=0)
    repository_added_bytes: int | None = Field(default=None, ge=0)
    repository_physical_bytes: int | None = Field(default=None, ge=0)
    repository_free_bytes: int | None = Field(default=None, ge=0)


class PublicJob(PublicModel):
    job_id: str
    display_name: str
    kind: Literal["snapshot", "mirror", "maintenance", "smart-test", "unknown"]
    health: Literal["healthy", "warning", "critical", "unknown"]
    health_reason: str
    protection_info: str | None = None
    next_fire_at: AwareDatetime | None = None
    next_operation: str | None = None
    deadline: str | None = None
    last_run: PublicRun | None = None
    previous_run: PublicRun | None = None
    last_success_at: AwareDatetime | None = None
    backup_metrics: PublicBackupMetrics | None = None


class PublicProgress(PublicModel):
    files_done: int | None = Field(default=None, ge=0)
    files_total: int | None = Field(default=None, ge=0)
    bytes_done: int | None = Field(default=None, ge=0)
    bytes_total: int | None = Field(default=None, ge=0)
    bytes_per_second: int | None = Field(default=None, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    updated_at: AwareDatetime | None = None


class PublicOperation(PublicModel):
    operation_id: UUID
    job_id: str
    position: int = Field(ge=0)
    job_display_name: str
    kind: str
    trigger_source: Literal["scheduled", "manual"]
    state: Literal["running", "queued"]
    stage: str | None = None
    elapsed_seconds: int = Field(ge=0)
    executor_state: Literal["running", "stopping", "exited", "not_started"]
    blocked_reason: str | None = None
    progress: PublicProgress | None = None


class PublicSmartMetric(PublicModel):
    current: int | bool | None = None
    previous: int | bool | None = None
    delta: int | None = None
    change_24h: int | None = None
    change_30d: int | None = None
    last_regression_at: AwareDatetime | None = None


class PublicSmartSelfTest(PublicModel):
    result: Literal["success", "failed", "timeout", "unsupported"]
    test_type: Literal["short", "long"]
    reason: str
    finished_at: AwareDatetime
    duration_seconds: int = Field(ge=0)
    remaining_percent: int | None = Field(default=None, ge=0, le=100)


class PublicDisk(PublicModel):
    disk_id: str
    manufacturer: str | None = None
    model: str | None = None
    media_type: Literal["hdd", "ssd", "nvme", "unknown"]
    bus_type: str | None = None
    capacity_bytes: int | None = Field(default=None, ge=0)
    role: str
    operational_state: Literal["online", "offline", "missing", "unknown"]
    smart_health: Literal["healthy", "warning", "critical", "unknown"]
    observed_at: AwareDatetime | None = None
    metrics: dict[str, PublicSmartMetric]
    last_self_test: PublicSmartSelfTest | None = None
    mount_points: tuple[str, ...] = ()
    passive_smart_health: Literal["healthy", "warning", "critical", "unknown"]
    affects_system_health: bool = True
    health_policy_reason: str | None = None
    stale: bool = False
    health_reasons: tuple[str, ...] = ()


class PublicVolume(PublicModel):
    volume_id: str
    display_name: str
    label: str | None = None
    filesystem: str | None = None
    disk_id: str
    role: str
    online: bool
    stale: bool
    total_bytes: int | None = Field(default=None, ge=0)
    used_bytes: int | None = Field(default=None, ge=0)
    free_bytes: int | None = Field(default=None, ge=0)
    free_percent: float | None = Field(default=None, ge=0, le=100)
    observed_at: AwareDatetime | None = None


class PublicHealthIssue(PublicModel):
    severity: Literal["warning", "critical"]
    kind: str
    subject: str
    summary: str


class StatusProjection(PublicModel):
    schema_version: Literal[1] = 1
    generation_id: UUID
    generated_at: AwareDatetime
    overall_health: Literal["healthy", "warning", "critical", "unknown"]
    backup_disk_state: Literal["offline", "online_during_backup", "error", "unknown"]
    operations: tuple[PublicOperation, ...]
    jobs: tuple[PublicJob, ...]
    disks: tuple[PublicDisk, ...]
    volumes: tuple[PublicVolume, ...]
    health_issues: tuple[PublicHealthIssue, ...]


class HealthProjection(PublicModel):
    schema_version: Literal[1] = 1
    generation_id: UUID
    generated_at: AwareDatetime
    manager_state: Literal["starting", "idle", "running", "stopping", "error"]
    manager_started_at: AwareDatetime
    version: str


class ProjectionPublisher:
    def __init__(self, public_directory: Path) -> None:
        self._public_directory = public_directory

    def publish(self, status: StatusProjection, health: HealthProjection) -> None:
        if status.generation_id != health.generation_id:
            raise ValueError("projection generation IDs must match")
        require_aware(status.generated_at)
        require_aware(health.generated_at)
        self._public_directory.mkdir(parents=True, exist_ok=True)
        self._replace_json(self._public_directory / "status.json", status)
        self._replace_json(self._public_directory / "health.json", health)

    @staticmethod
    def _replace_json(path: Path, value: PublicModel) -> None:
        temporary = path.with_name(f".{path.name}.{value.model_dump()['generation_id']}.tmp")
        data = json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
