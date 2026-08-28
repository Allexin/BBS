"""Production composition for manual mirror and snapshot restore."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from backup_system.common.config import MirrorJobConfig, SmartConfig, SnapshotJobConfig
from backup_system.common.events import (
    EventBase,
    RestoreCompleted,
    RestoreTargetReady,
    StageChanged,
)
from backup_system.common.time import utc_now
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_restore import MirrorRestore
from backup_system.executor.mirror_win32 import WindowsMirrorFileOperations
from backup_system.executor.restic_process import ResticProcess
from backup_system.executor.restore_request import load_restore_request
from backup_system.executor.runtime import build_windows_job
from backup_system.executor.smart_preflight import SmartPreflightObservation
from backup_system.executor.snapshot_restore import SnapshotRestore


def run_restore_operation(
    *,
    runtime_root: Path,
    config: SnapshotJobConfig | MirrorJobConfig,
    smart_config: SmartConfig,
    request_file: Path,
    cancellation: CancellationToken,
    smart_sink: Callable[[tuple[SmartPreflightObservation, ...]], None],
    event_sink: Callable[[EventBase], None],
) -> object:
    if not request_file.is_absolute():
        raise ValueError("restore request file must be absolute")
    request = load_restore_request(request_file, expected_job_id=config.id)

    def stage(name: str) -> None:
        event_sink(StageChanged(event="stage_changed", timestamp=utc_now(), stage=name))

    def ready(path: Path) -> None:
        event_sink(
            RestoreTargetReady(
                event="restore_target_ready",
                timestamp=utc_now(),
                result_path=str(path),
            )
        )

    windows_job = build_windows_job(
        runtime_root=runtime_root,
        cancellation=cancellation,
        smart_sink=smart_sink,
    )

    def restore() -> object:
        if isinstance(config, MirrorJobConfig):
            operations = WindowsMirrorFileOperations()

            def copy_file(source: Path, destination: Path, size_bytes: int) -> None:
                operations.copy_to_temp(
                    source,
                    destination,
                    expected_size=size_bytes,
                    cancellation=cancellation,
                )

            mirror_restore = MirrorRestore(
                cancellation=cancellation,
                copy_file=copy_file,
                stage_sink=stage,
                ready_sink=ready,
            )
            result = mirror_restore.run(
                destination_root=Path(config.destination.path),
                source_root=Path(config.source.path),
                request=request,
                job_id=config.id,
                marker_uuid=config.destination.marker_uuid,
            )
        else:
            snapshot_restore = SnapshotRestore(
                runner=ResticProcess(runtime_root / "bin" / "restic.exe", cancellation),
                cancellation=cancellation,
                secret_directory=runtime_root / "data" / "state" / "executor" / "secrets",
                stage_sink=stage,
                ready_sink=ready,
            )
            result = snapshot_restore.run(config, request)
        event_sink(
            RestoreCompleted(
                event="restore_completed",
                timestamp=utc_now(),
                result_path=str(result.result_path),
                files_restored=result.files_restored,
                logical_bytes=result.logical_bytes,
            )
        )
        return result

    return windows_job.run_destination(
        config=config,
        smart_config=smart_config,
        adapter=lambda context: restore(),
    )
