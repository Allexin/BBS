"""Manual recovery use case for exact owned-VSS cleanup and disk offline."""

from __future__ import annotations

from dataclasses import dataclass

from backup_system.common.config import DiskConfig
from backup_system.executor.coordinator import ExecutorWindowsCoordinator
from backup_system.executor.vss_intent import SnapshotSetCleaner, VssIntentStore


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    vss_intent_cleaned: bool
    disk_offline_confirmed: bool


class ExecutorRecovery:
    def __init__(
        self,
        *,
        coordinator: ExecutorWindowsCoordinator,
        intents: VssIntentStore,
        vss_cleaner: SnapshotSetCleaner,
    ) -> None:
        self._coordinator = coordinator
        self._intents = intents
        self._vss_cleaner = vss_cleaner

    def run(self, *, job_id: str, disk: DiskConfig) -> RecoveryResult:
        cleaned = False

        def cleanup() -> None:
            nonlocal cleaned
            cleaned = self._intents.recover(job_id, self._vss_cleaner)

        lifecycle = self._coordinator.recover(
            disk=disk,
            owned_vss_cleanup=cleanup,
        )
        return RecoveryResult(
            vss_intent_cleaned=cleaned,
            disk_offline_confirmed=lifecycle.disk_offline_confirmed,
        )
