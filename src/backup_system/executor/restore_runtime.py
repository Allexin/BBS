"""Production composition for manual mirror and snapshot restore."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from backup_system.common.config import MirrorJobConfig, SmartConfig, SnapshotJobConfig
from backup_system.common.events import (
    EventBase,
    Progress,
    RestoreCompleted,
    RestoreTargetReady,
    RestoreVersionResolved,
    StageChanged,
)
from backup_system.common.time import utc_now
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_restore import MirrorRestore
from backup_system.executor.mirror_win32 import WindowsMirrorFileOperations
from backup_system.executor.restic_auth import restic_auth_arguments
from backup_system.executor.restic_process import ResticProcess
from backup_system.executor.restore_request import RestoreRequest, load_restore_request
from backup_system.executor.restore_target import RestoreTargetError
from backup_system.executor.runtime import build_windows_job
from backup_system.executor.smart_preflight import SmartPreflightObservation
from backup_system.executor.snapshot_restore import SnapshotRestore, resolve_snapshot_version


def run_restore_resolution_operation(
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
    windows_job = build_windows_job(
        runtime_root=runtime_root,
        cancellation=cancellation,
        smart_sink=smart_sink,
    )

    def resolve() -> str:
        if isinstance(config, MirrorJobConfig):
            if request.version != "latest":
                raise RestoreTargetError("mirror restore supports only version latest")
            version = "latest"
        else:
            runner = ResticProcess(runtime_root / "bin" / "restic.exe", cancellation)
            runner.verify_version()
            with restic_auth_arguments(
                config.repository.encryption,
                runtime_root / "data" / "state" / "executor" / "secrets",
            ) as auth:
                version = resolve_snapshot_version(
                    runner,
                    ("--repo", config.repository.path, *auth),
                    config,
                    request.version,
                )
        event_sink(
            RestoreVersionResolved(
                event="restore_version_resolved",
                timestamp=utc_now(),
                version=version,
            )
        )
        return version

    return windows_job.run_destination(
        config=config,
        smart_config=smart_config,
        adapter=lambda context: resolve(),
    )


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

    def progress(
        stage_name: str,
        files_done: int,
        files_total: int,
        bytes_done: int,
        bytes_total: int,
    ) -> None:
        event_sink(
            Progress(
                event="progress",
                timestamp=utc_now(),
                stage=stage_name,
                files_done=files_done,
                files_total=files_total,
                bytes_done=bytes_done,
                bytes_total=bytes_total,
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
                progress_sink=progress,
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
                progress_sink=progress,
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


def run_restore_test_operation(
    *,
    runtime_root: Path,
    config: SnapshotJobConfig | MirrorJobConfig,
    smart_config: SmartConfig,
    cancellation: CancellationToken,
    smart_sink: Callable[[tuple[SmartPreflightObservation, ...]], None],
    event_sink: Callable[[EventBase], None],
) -> tuple[object, ...]:
    configured_paths = config.verification.restore_test_paths
    if not configured_paths:
        raise RestoreTargetError("restore-test has no configured control paths")
    target = runtime_root / "data" / "restore-tests"
    target.mkdir(parents=True, exist_ok=True)
    temporary_directory = runtime_root / "data" / "temp"
    temporary_directory.mkdir(parents=True, exist_ok=True)
    outcomes: list[object] = []
    for selected in configured_paths:
        relative = _relative_control_path(config.source.path, selected)
        request = RestoreRequest.model_construct(
            schema_version=1,
            request_id=uuid4(),
            job_id=config.id,
            version="latest",
            path=relative,
            target=str(target),
        )
        request_file = temporary_directory / f"restore-test-{request.request_id}.json"
        try:
            with request_file.open("xb", encoding="utf-8", newline="\n") as stream:
                stream.write(request.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            outcomes.append(
                run_restore_operation(
                    runtime_root=runtime_root,
                    config=config,
                    smart_config=smart_config,
                    request_file=request_file,
                    cancellation=cancellation,
                    smart_sink=smart_sink,
                    event_sink=event_sink,
                )
            )
        finally:
            request_file.unlink(missing_ok=True)
    return tuple(outcomes)


def _relative_control_path(source: str, selected: str) -> str:
    source_path = PureWindowsPath(source)
    selected_path = PureWindowsPath(selected)
    try:
        relative = selected_path.relative_to(source_path)
    except ValueError as error:
        raise RestoreTargetError("restore-test path is outside source root") from error
    value = str(relative)
    return value if value else "."
