"""YAML loading and cross-file validation without runtime side effects."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Any

import yaml
from pydantic import ValidationError

from backup_system.common.config import (
    EXECUTOR_JOB_CONFIG_ADAPTER,
    JOB_ID_PATTERN,
    ExecutorJobConfig,
    MaintenanceJobConfig,
    ManagerConfig,
    MirrorJobConfig,
    SmartConfig,
    SmartTestJobConfig,
    SnapshotJobConfig,
)


class ConfigLoadError(ValueError):
    """A configuration file could not be read or validated."""


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigLoadError(f"cannot read config {path}: {error}") from error
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigLoadError(f"invalid YAML in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigLoadError(f"config {path} must contain a YAML mapping")
    return value


def load_manager_config(path: Path) -> ManagerConfig:
    try:
        return ManagerConfig.model_validate(_load_yaml_mapping(path))
    except ValidationError as error:
        raise ConfigLoadError(f"invalid manager config {path}: {error}") from error


def load_smart_config(path: Path) -> SmartConfig:
    try:
        return SmartConfig.model_validate(_load_yaml_mapping(path))
    except ValidationError as error:
        raise ConfigLoadError(f"invalid SMART config {path}: {error}") from error


def load_job_config(path: Path) -> ExecutorJobConfig:
    try:
        return EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(_load_yaml_mapping(path))
    except ValidationError as error:
        raise ConfigLoadError(f"invalid executor job config {path}: {error}") from error


def job_config_path(config_dir: Path, job_id: str) -> Path:
    """Resolve an already schema-valid job ID without globbing or path search."""
    return config_dir / "jobs" / f"{job_id}.yaml"


def validate_job_with_owner(config_dir: Path, job_id: str) -> ExecutorJobConfig:
    if JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ConfigLoadError("invalid job ID")
    config = load_job_config(job_config_path(config_dir, job_id))
    if config.id != job_id:
        raise ConfigLoadError("job config ID must match its filename")
    if not isinstance(config, MaintenanceJobConfig):
        return config

    owner = load_job_config(job_config_path(config_dir, config.repository_owner_job_id))
    if not isinstance(owner, SnapshotJobConfig):
        raise ConfigLoadError("maintenance owner must be a snapshot job")
    if owner.id != config.repository_owner_job_id:
        raise ConfigLoadError("maintenance owner ID must match its filename")
    if config.repository != owner.repository or config.disk != owner.disk:
        raise ConfigLoadError("maintenance repository and disk must exactly match its owner")
    return config


def validate_config_tree(manager_path: Path) -> tuple[ManagerConfig, SmartConfig]:
    manager = load_manager_config(manager_path)
    config_dir = manager_path.parent
    smart = load_smart_config(config_dir / "smart.yaml")
    executor_jobs = {job.id: validate_job_with_owner(config_dir, job.id) for job in manager.jobs}
    for manager_job in manager.jobs:
        executor_job = executor_jobs[manager_job.id]
        if isinstance(executor_job, MaintenanceJobConfig):
            allowed = {"prune"}
        elif isinstance(executor_job, SmartTestJobConfig):
            allowed = {"smart-test"}
        else:
            allowed = {"backup", "check"}
        configured = {item.operation for item in manager_job.schedule.cycle}
        if not configured <= allowed:
            raise ConfigLoadError(
                f"manager cycle for {manager_job.id} is incompatible with {executor_job.kind}"
            )
        if (
            isinstance(executor_job, SmartTestJobConfig)
            and executor_job.target.mode == "configured-disk"
            and not any(disk.id == executor_job.target.disk_id for disk in smart.disks)
        ):
            raise ConfigLoadError(
                f"SMART test job {executor_job.id} references a disk outside SMART allowlist"
            )

    snapshots = [job for job in executor_jobs.values() if isinstance(job, SnapshotJobConfig)]
    for index, left in enumerate(snapshots):
        for right in snapshots[index + 1 :]:
            if (
                left.repository.repository_id == right.repository.repository_id
                or left.repository.path.casefold() == right.repository.path.casefold()
                or left.repository.marker_uuid == right.repository.marker_uuid
            ):
                raise ConfigLoadError("snapshot jobs must own distinct repositories")
    return manager, smart


def config_validation_warnings(config_dir: Path, manager: ManagerConfig) -> tuple[str, ...]:
    messages = job_protection_info(config_dir, manager).values()
    return tuple(f"WARNING: {message}" for message in messages)


def job_protection_info(config_dir: Path, manager: ManagerConfig) -> dict[str, str]:
    info: dict[str, str] = {}
    for registered in manager.jobs:
        config = load_job_config(job_config_path(config_dir, registered.id))
        if isinstance(config, SnapshotJobConfig):
            destination = config.repository.path
        elif isinstance(config, MirrorJobConfig):
            destination = config.destination.path
        else:
            continue
        if PureWindowsPath(config.source.path).drive.casefold() == PureWindowsPath(
            destination
        ).drive.casefold():
            info[config.id] = (
                f"job {config.id!r} stores its backup on the source volume; "
                "it protects against file loss but not physical disk failure"
            )
    return info
