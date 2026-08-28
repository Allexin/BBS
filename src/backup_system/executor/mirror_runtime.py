"""Production composition for mirror executor operations."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from uuid import UUID

from backup_system.common.config import MirrorJobConfig, SmartConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_adapter import MirrorAdapter
from backup_system.executor.mirror_win32 import WindowsMirrorFileOperations
from backup_system.executor.runtime import build_windows_job
from backup_system.executor.smart_preflight import SmartPreflightObservation
from backup_system.executor.source_volume import SourceVolumeResolver
from backup_system.executor.storage_inventory import WindowsStorageInventory
from backup_system.executor.windows_job import WindowsDataContext


class MirrorRuntimeError(RuntimeError):
    pass


def run_mirror_operation(
    *,
    runtime_root: Path,
    config: MirrorJobConfig,
    smart_config: SmartConfig,
    run_id: UUID,
    operation: str,
    mode: str | None,
    cancellation: CancellationToken,
    smart_sink: Callable[[tuple[SmartPreflightObservation, ...]], None],
) -> object:
    destination = _validated_destination(config)
    adapter = MirrorAdapter(
        files=WindowsMirrorFileOperations(),
        cancellation=cancellation,
    )
    windows_job = build_windows_job(
        runtime_root=runtime_root,
        cancellation=cancellation,
        smart_sink=smart_sink,
    )

    if operation == "check":
        if mode is None:
            raise MirrorRuntimeError("mirror check requires a mode")
        return windows_job.run_destination(
            config=config,
            smart_config=smart_config,
            adapter=lambda context: adapter.check(
                destination_root=destination,
                job_id=config.id,
                marker_uuid=config.destination.marker_uuid,
                mode=mode,
            ),
        )
    if operation not in {"run", "repair-mirror"}:
        raise MirrorRuntimeError(f"unsupported mirror operation: {operation}")
    _require_distinct_source_disk(config)

    def reconcile(context: WindowsDataContext) -> object:
        result = adapter.backup(
            source_root=Path(context.source_root),
            destination_root=destination,
            excludes=config.excludes,
            job_id=config.id,
            marker_uuid=config.destination.marker_uuid,
            run_id=run_id,
            volume_free_bytes=shutil.disk_usage(destination).free,
        )
        if operation == "repair-mirror":
            adapter.check(
                destination_root=destination,
                job_id=config.id,
                marker_uuid=config.destination.marker_uuid,
                mode="full",
            )
        return result

    return windows_job.run(
        config=config,
        smart_config=smart_config,
        run_id=run_id,
        adapter=reconcile,
    )


def _validated_destination(config: MirrorJobConfig) -> Path:
    mount = PureWindowsPath(config.disk.mount_point)
    destination = PureWindowsPath(config.destination.path)
    mount_parts = tuple(part.casefold() for part in mount.parts)
    destination_parts = tuple(part.casefold() for part in destination.parts)
    if (
        destination == mount
        or destination_parts[: len(mount_parts)] != mount_parts
    ):
        raise MirrorRuntimeError("mirror destination must be inside the verified backup mount")
    return Path(destination)


def _require_distinct_source_disk(config: MirrorJobConfig) -> None:
    source = SourceVolumeResolver().resolve(config.source.path)
    matching = [
        candidate
        for candidate in WindowsStorageInventory().enumerate()
        if candidate.volume_guid.strip("{}").casefold() == str(source.volume_guid).casefold()
    ]
    if len(matching) != 1:
        raise MirrorRuntimeError("source physical disk identity could not be established")
    if matching[0].physical_serial.casefold() == config.disk.physical_serial.casefold():
        raise MirrorRuntimeError("source and mirror destination cannot use the same physical disk")
