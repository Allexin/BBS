"""Production composition for snapshot and repository maintenance operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import UUID

from backup_system.common.config import MaintenanceJobConfig, SmartConfig, SnapshotJobConfig
from backup_system.common.events import EventBase, Progress, SnapshotCreated, StageChanged
from backup_system.common.time import utc_now
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.restic_process import ResticProcess
from backup_system.executor.runtime import build_windows_job
from backup_system.executor.smart_preflight import SmartPreflightObservation
from backup_system.executor.snapshot_adapter import SnapshotAdapter
from backup_system.executor.snapshot_state import SnapshotStateStore
from backup_system.executor.source_volume import SourceVolumeResolver
from backup_system.executor.storage_inventory import WindowsStorageInventory


class SnapshotRuntimeError(RuntimeError):
    pass


def run_snapshot_operation(
    *,
    runtime_root: Path,
    config: SnapshotJobConfig | MaintenanceJobConfig,
    smart_config: SmartConfig,
    run_id: UUID,
    operation: str,
    mode: str | None,
    cancellation: CancellationToken,
    smart_sink: Callable[[tuple[SmartPreflightObservation, ...]], None],
    event_sink: Callable[[EventBase], None],
) -> object:
    _validated_repository(config)
    state_root = runtime_root / "data" / "state" / "executor"

    def stage(name: str) -> None:
        event_sink(StageChanged(event="stage_changed", timestamp=utc_now(), stage=name))

    adapter = SnapshotAdapter(
        runner=ResticProcess(
            runtime_root / "bin" / "restic.exe",
            cancellation,
            event_sink=lambda event: _emit_progress(event, event_sink),
        ),
        states=SnapshotStateStore(
            state_root,
            runtime_root / "data" / "diagnostics" / "snapshot-state",
        ),
        secret_directory=state_root / "secrets",
        stage_sink=stage,
        snapshot_sink=lambda snapshot_id, bytes_added: event_sink(
            SnapshotCreated(
                event="snapshot_created",
                timestamp=utc_now(),
                snapshot_id=snapshot_id,
                bytes_added=bytes_added,
            )
        ),
    )
    windows_job = build_windows_job(
        runtime_root=runtime_root,
        cancellation=cancellation,
        smart_sink=smart_sink,
    )
    if isinstance(config, MaintenanceJobConfig):
        if operation != "prune":
            raise SnapshotRuntimeError("maintenance job supports only prune here")
        return windows_job.run_repository(
            config=config,
            smart_config=smart_config,
            adapter=lambda volume: adapter.prune(config),
        )
    if operation == "check":
        if mode is None:
            raise SnapshotRuntimeError("snapshot check requires a mode")
        return windows_job.run_destination(
            config=config,
            smart_config=smart_config,
            adapter=lambda context: adapter.check(config, mode=mode),
        )
    if operation != "run":
        raise SnapshotRuntimeError(f"unsupported snapshot operation: {operation}")
    _require_distinct_source_disk(config)
    return windows_job.run(
        config=config,
        smart_config=smart_config,
        run_id=run_id,
        adapter=lambda context: adapter.backup(config, source_root=Path(context.source_root)),
    )


def _validated_repository(config: SnapshotJobConfig | MaintenanceJobConfig) -> Path:
    mount = PureWindowsPath(config.disk.mount_point)
    repository = PureWindowsPath(config.repository.path)
    mount_parts = tuple(part.casefold() for part in mount.parts)
    repository_parts = tuple(part.casefold() for part in repository.parts)
    if repository == mount or repository_parts[: len(mount_parts)] != mount_parts:
        raise SnapshotRuntimeError("snapshot repository must be inside the verified backup mount")
    return Path(repository)


def _require_distinct_source_disk(config: SnapshotJobConfig) -> None:
    source = SourceVolumeResolver().resolve(config.source.path)
    matching = [
        candidate
        for candidate in WindowsStorageInventory().enumerate()
        if candidate.volume_guid.strip("{}").casefold() == str(source.volume_guid).casefold()
    ]
    if len(matching) != 1:
        raise SnapshotRuntimeError("source physical disk identity could not be established")
    if matching[0].physical_serial.casefold() == config.disk.physical_serial.casefold():
        raise SnapshotRuntimeError("source and snapshot destination cannot use the same disk")


def _emit_progress(
    event: Mapping[str, Any], sink: Callable[[EventBase], None]
) -> None:
    if event.get("message_type") != "status":
        return
    sink(
        Progress(
            event="progress",
            timestamp=utc_now(),
            stage="backing_up",
            files_done=_optional_nonnegative_int(event.get("files_done")),
            files_total=_optional_nonnegative_int(event.get("total_files")),
            bytes_done=_optional_nonnegative_int(event.get("bytes_done")),
            bytes_total=_optional_nonnegative_int(event.get("total_bytes")),
        )
    )


def _optional_nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
