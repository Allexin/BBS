"""Production dependency composition for executor Windows lifecycle operations."""

from __future__ import annotations

from pathlib import Path

from backup_system.common.config import ExecutorJobConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.coordinator import ExecutorWindowsCoordinator
from backup_system.executor.disk_control import DiskController
from backup_system.executor.lifecycle import ExecutorDiskLifecycle
from backup_system.executor.machine_lock import MachineExecutorLock
from backup_system.executor.native_vss import NativeVssBackend
from backup_system.executor.recovery import ExecutorRecovery, RecoveryResult
from backup_system.executor.smart_preflight import SmartPreflight, SubprocessSmartctlBackend
from backup_system.executor.vss_intent import VssIntentStore
from backup_system.executor.win32_storage import Win32StorageBackend


def run_recovery(
    *,
    runtime_root: Path,
    config: ExecutorJobConfig,
    cancellation: CancellationToken,
) -> RecoveryResult:
    executor_state = runtime_root / "data" / "state" / "executor"
    coordinator = ExecutorWindowsCoordinator(
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
        smart_sink=lambda observations: None,
        cancellation=cancellation,
    )
    return ExecutorRecovery(
        coordinator=coordinator,
        intents=VssIntentStore(executor_state),
        vss_cleaner=NativeVssBackend(cancellation_checkpoint=cancellation.raise_if_requested),
    ).run(job_id=config.id, disk=config.disk)
