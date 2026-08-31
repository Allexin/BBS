"""Production dependency composition for executor Windows lifecycle operations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from backup_system.common.config import MaintenanceJobConfig, MirrorJobConfig, SnapshotJobConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.coordinator import ExecutorWindowsCoordinator
from backup_system.executor.disk_control import DiskController
from backup_system.executor.lifecycle import ExecutorDiskLifecycle
from backup_system.executor.machine_lock import MachineExecutorLock
from backup_system.executor.native_vss import NativeVssBackend
from backup_system.executor.recovery import ExecutorRecovery, RecoveryResult
from backup_system.executor.smart_preflight import (
    SmartPreflight,
    SmartPreflightObservation,
    SubprocessSmartctlBackend,
)
from backup_system.executor.source_snapshot import ExecutorSourceSnapshot
from backup_system.executor.source_volume import SourceVolumeResolver
from backup_system.executor.vss_intent import OwnedVssSnapshotManager, VssIntentStore
from backup_system.executor.win32_storage import Win32StorageBackend
from backup_system.executor.windows_job import ExecutorWindowsJob


def build_windows_job(
    *,
    runtime_root: Path,
    cancellation: CancellationToken,
    smart_sink: Callable[[tuple[SmartPreflightObservation, ...]], None],
) -> ExecutorWindowsJob:
    executor_state = runtime_root / "data" / "state" / "executor"
    vss = NativeVssBackend(cancellation_checkpoint=cancellation.raise_if_requested)
    coordinator = _build_coordinator(
        runtime_root=runtime_root,
        executor_state=executor_state,
        cancellation=cancellation,
        smart_sink=smart_sink,
    )
    return ExecutorWindowsJob(
        coordinator=coordinator,
        source_snapshots=ExecutorSourceSnapshot(
            resolver=SourceVolumeResolver(),
            snapshots=OwnedVssSnapshotManager(vss, VssIntentStore(executor_state / "vss-intents")),
            cancellation_checkpoint=cancellation.raise_if_requested,
        ),
    )


def run_recovery(
    *,
    runtime_root: Path,
    config: SnapshotJobConfig | MirrorJobConfig | MaintenanceJobConfig,
    cancellation: CancellationToken,
) -> RecoveryResult:
    executor_state = runtime_root / "data" / "state" / "executor"
    coordinator = _build_coordinator(
        runtime_root=runtime_root,
        executor_state=executor_state,
        cancellation=cancellation,
        smart_sink=lambda observations: None,
    )
    return ExecutorRecovery(
        coordinator=coordinator,
        intents=VssIntentStore(executor_state / "vss-intents"),
        vss_cleaner=NativeVssBackend(cancellation_checkpoint=cancellation.raise_if_requested),
    ).run(job_id=config.id, disk=config.disk)


def _build_coordinator(
    *,
    runtime_root: Path,
    executor_state: Path,
    cancellation: CancellationToken,
    smart_sink: Callable[[tuple[SmartPreflightObservation, ...]], None],
) -> ExecutorWindowsCoordinator:
    return ExecutorWindowsCoordinator(
        lock_factory=lambda: MachineExecutorLock(executor_state / "machine.lock"),
        disk_lifecycle=ExecutorDiskLifecycle(
            DiskController(
                Win32StorageBackend(),
                cancellation_checkpoint=cancellation.raise_if_requested,
            )
        ),
        smart=SmartPreflight(
            SubprocessSmartctlBackend(runtime_root / "bin" / "smartctl.exe"),
            cancellation_checkpoint=cancellation.raise_if_requested,
        ),
        smart_sink=smart_sink,
        cancellation=cancellation,
    )
