"""Owned VSS lifecycle exposing only a verified shadow source root to adapters."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Protocol, TypeVar
from uuid import UUID

from backup_system.executor.source_volume import ResolvedSourceVolume
from backup_system.executor.vss import VssSnapshot
from backup_system.executor.vss_intent import OwnedVssSnapshotManager

T = TypeVar("T")


class SourceSnapshotError(RuntimeError):
    pass


class SourceResolver(Protocol):
    def resolve(self, source_path: str) -> ResolvedSourceVolume: ...


class ExecutorSourceSnapshot:
    def __init__(
        self,
        *,
        resolver: SourceResolver,
        snapshots: OwnedVssSnapshotManager,
        cancellation_checkpoint: Callable[[], None],
        directory_check: Callable[[PureWindowsPath], bool] | None = None,
    ) -> None:
        self._resolver = resolver
        self._snapshots = snapshots
        self._cancellation_checkpoint = cancellation_checkpoint
        self._directory_check = directory_check or (lambda path: Path(path).is_dir())

    def run(
        self,
        *,
        job_id: str,
        run_id: UUID,
        source_path: str,
        action: Callable[[PureWindowsPath], T],
    ) -> T:
        self._cancellation_checkpoint()
        source = self._resolver.resolve(source_path)
        self._cancellation_checkpoint()

        def use_snapshot(snapshot: VssSnapshot) -> T:
            shadow_root = source.shadow_root(snapshot.shadow_device_path)
            if not self._directory_check(shadow_root):
                raise SourceSnapshotError("source root is not readable inside VSS snapshot")
            self._cancellation_checkpoint()
            value = action(shadow_root)
            self._cancellation_checkpoint()
            return value

        return self._snapshots.run(
            job_id=job_id,
            run_id=run_id,
            volume_guid=str(source.volume_guid),
            action=use_snapshot,
        )
