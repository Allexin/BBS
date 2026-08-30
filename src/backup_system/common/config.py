"""Strict, versioned configuration contracts shared by component boundaries."""

from __future__ import annotations

import re
from pathlib import PureWindowsPath
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

JOB_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_job_id(value: str) -> str:
    if JOB_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("must match ^[a-z][a-z0-9-]{0,62}$")
    return value


def validate_job_id(value: str) -> str:
    """Validate a public job identifier at CLI and file boundaries."""
    return _validate_job_id(value)


def _validate_absolute_windows_path(value: str) -> str:
    path = PureWindowsPath(value)
    if not path.is_absolute() or not path.drive:
        raise ValueError("must be an absolute Windows path")
    return value


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError("must be a valid IANA timezone") from error
    return value


class CycleItem(StrictModel):
    operation: Literal["backup", "check", "prune", "smart-test"]
    mode: Literal["metadata", "subset", "full"] | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> CycleItem:
        if self.operation == "check" and self.mode is None:
            raise ValueError("check cycle item requires mode")
        if self.operation != "check" and self.mode is not None:
            raise ValueError("mode is allowed only for check")
        return self


class ScheduleConfig(StrictModel):
    cron: str
    timezone: str
    deadline: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    cycle: tuple[CycleItem, ...] = Field(min_length=1)

    _valid_timezone = field_validator("timezone")(_validate_timezone)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value: str) -> str:
        if not croniter.is_valid(value):
            raise ValueError("invalid cron expression")
        return value


class ManagerJobConfig(StrictModel):
    id: str
    enabled: bool
    display_name: str = Field(min_length=1)
    schedule: ScheduleConfig

    _job_id = field_validator("id")(_validate_job_id)


class MonitoredVolumeConfig(StrictModel):
    id: str
    display_name: str = Field(min_length=1)
    volume_guid: str = Field(min_length=1)

    _volume_id = field_validator("id")(_validate_job_id)


class VolumeMonitoringConfig(StrictModel):
    poll_seconds: int = Field(gt=0)
    items: tuple[MonitoredVolumeConfig, ...]


class MonitoringConfig(StrictModel):
    volumes: VolumeMonitoringConfig


class SchedulerConfig(StrictModel):
    poll_seconds: int = Field(gt=0)


class TelegramConfig(StrictModel):
    enabled: bool
    credentials_file: str = Field(min_length=1)
    daily_report_cron: str
    daily_report_timezone: str
    stale_manager_minutes: int = Field(gt=0)

    _valid_cron = field_validator("daily_report_cron")(ScheduleConfig.validate_cron)
    _valid_timezone = field_validator("daily_report_timezone")(_validate_timezone)

    @field_validator("credentials_file")
    @classmethod
    def validate_credentials_file(cls, value: str) -> str:
        path = PureWindowsPath(value)
        if path.name != value or path.suffix.casefold() != ".json":
            raise ValueError("must be a JSON filename without a path")
        return value


class ManagerConfig(StrictModel):
    schema_version: Literal[1] = 1
    timezone: str
    scheduler: SchedulerConfig
    monitoring: MonitoringConfig
    jobs: tuple[ManagerJobConfig, ...]
    telegram: TelegramConfig

    _valid_timezone = field_validator("timezone")(_validate_timezone)

    @model_validator(mode="after")
    def unique_ids(self) -> ManagerConfig:
        job_ids = [item.id.casefold() for item in self.jobs]
        volume_ids = [item.id.casefold() for item in self.monitoring.volumes.items]
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("manager job IDs must be unique")
        if len(volume_ids) != len(set(volume_ids)):
            raise ValueError("monitored volume IDs must be unique")
        return self


class SourceConfig(StrictModel):
    path: str

    _absolute_path = field_validator("path")(_validate_absolute_windows_path)


class EncryptionConfig(StrictModel):
    mode: Literal["none", "password"]
    passphrase: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_passphrase(self) -> EncryptionConfig:
        if self.mode == "password" and self.passphrase is None:
            raise ValueError("password mode requires passphrase")
        if self.mode == "none" and self.passphrase is not None:
            raise ValueError("none mode forbids passphrase")
        return self


class RepositoryConfig(StrictModel):
    engine: Literal["restic"]
    repository_id: str
    path: str
    marker_uuid: UUID
    encryption: EncryptionConfig
    marker_file: str

    _repository_id = field_validator("repository_id")(_validate_job_id)
    _absolute_paths = field_validator("path", "marker_file")(_validate_absolute_windows_path)


class DiskConfig(StrictModel):
    physical_serial: str = Field(min_length=1)
    expected_size_bytes: int = Field(gt=0)
    partition_guid: str = Field(min_length=1)
    volume_guid: str = Field(min_length=1)
    mount_point: str
    repository_path_timeout_seconds: int = Field(gt=0)

    _absolute_mount_point = field_validator("mount_point")(_validate_absolute_windows_path)


class SnapshotBackupConfig(StrictModel):
    host: str = Field(min_length=1)
    tags: tuple[str, ...]
    read_error_result: Literal["failed"]


class SnapshotRetentionConfig(StrictModel):
    keep_last: int = Field(ge=0)
    keep_daily: int = Field(ge=0)
    keep_weekly: int = Field(ge=0)
    keep_monthly: int = Field(ge=0)
    keep_yearly: int = Field(ge=0)


class VerificationConfig(StrictModel):
    restore_test_paths: tuple[str, ...]

    _absolute_paths = field_validator("restore_test_paths")(
        lambda values: tuple(_validate_absolute_windows_path(value) for value in values)
    )


class SnapshotVerificationConfig(VerificationConfig):
    data_subset_parts: int = Field(gt=0)


class DestinationConfig(StrictModel):
    path: str
    marker_file: str
    marker_uuid: UUID

    _absolute_paths = field_validator("path", "marker_file")(_validate_absolute_windows_path)


class JobBase(StrictModel):
    schema_version: Literal[1] = 1
    id: str
    kind: str
    display_name: str = Field(min_length=1)

    _job_id = field_validator("id")(_validate_job_id)


class DataJobBase(JobBase):
    source: SourceConfig
    excludes: tuple[str, ...]
    disk: DiskConfig

    @field_validator("excludes")
    @classmethod
    def validate_excludes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        keys: set[str] = set()
        for value in values:
            path = PureWindowsPath(value)
            if not value or path.is_absolute() or path.drive or ".." in path.parts:
                raise ValueError("excludes must be non-empty relative Windows paths")
            if value.startswith(("\\", "/")) or any(char in value for char in "*?"):
                raise ValueError("excludes cannot contain roots or wildcards")
            key = str(path).casefold()
            if key in keys:
                raise ValueError("excludes must be case-insensitively unique")
            keys.add(key)
        return values


class SnapshotJobConfig(DataJobBase):
    kind: Literal["snapshot"]
    repository: RepositoryConfig
    backup: SnapshotBackupConfig
    retention: SnapshotRetentionConfig
    verification: SnapshotVerificationConfig


class MirrorJobConfig(DataJobBase):
    kind: Literal["mirror"]
    destination: DestinationConfig
    verification: VerificationConfig


class MaintenanceJobConfig(JobBase):
    kind: Literal["maintenance"]
    repository_owner_job_id: str
    repository: RepositoryConfig
    disk: DiskConfig

    _owner_id = field_validator("repository_owner_job_id")(_validate_job_id)


class ConfiguredSmartTarget(StrictModel):
    mode: Literal["configured-disk"]
    disk_id: str

    _disk_id = field_validator("disk_id")(_validate_job_id)


class AllSystemSmartTarget(StrictModel):
    mode: Literal["all-system"]


SmartTestTarget = Annotated[
    ConfiguredSmartTarget | AllSystemSmartTarget, Field(discriminator="mode")
]


class SmartTestJobConfig(JobBase):
    kind: Literal["smart-test"]
    target: SmartTestTarget
    test_type: Literal["short", "long"]
    poll_seconds: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)

ExecutorJobConfig = Annotated[
    SnapshotJobConfig | MirrorJobConfig | MaintenanceJobConfig | SmartTestJobConfig,
    Field(discriminator="kind"),
]
EXECUTOR_JOB_CONFIG_ADAPTER: TypeAdapter[ExecutorJobConfig] = TypeAdapter(ExecutorJobConfig)


class SmartDiskIdentityConfig(StrictModel):
    device: str = Field(pattern=r"^/dev/pd[0-9]+$")
    serial: str = Field(min_length=1)
    expected_size_bytes: int = Field(gt=0)


class SmartDiskConfig(StrictModel):
    id: str
    display_name: str = Field(min_length=1)
    identity: SmartDiskIdentityConfig

    _disk_id = field_validator("id")(_validate_job_id)


class SmartConfig(StrictModel):
    schema_version: Literal[1] = 1
    per_disk_timeout_seconds: int = Field(gt=0)
    stale_after_hours: int = Field(gt=0)
    disks: tuple[SmartDiskConfig, ...]

    @model_validator(mode="after")
    def unique_disks(self) -> SmartConfig:
        ids = [disk.id.casefold() for disk in self.disks]
        serials = [disk.identity.serial.casefold() for disk in self.disks]
        devices = [disk.identity.device.casefold() for disk in self.disks]
        if (
            len(ids) != len(set(ids))
            or len(serials) != len(set(serials))
            or len(devices) != len(set(devices))
        ):
            raise ValueError("SMART disk IDs, serials, and devices must be unique")
        return self
