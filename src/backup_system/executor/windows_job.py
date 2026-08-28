"""Complete stage-5 Windows boundary for future data adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Protocol, TypeVar
from uuid import UUID

from backup_system.common.config import MirrorJobConfig, SmartConfig, SnapshotJobConfig
from backup_system.executor.coordinator import ExecutorWindowsCoordinator, ExecutorWindowsResult
from backup_system.executor.disk_control import VolumeObservation
from backup_system.executor.lifecycle import MarkerExpectation

T = TypeVar("T")
DataJobConfig = SnapshotJobConfig | MirrorJobConfig


@dataclass(frozen=True, slots=True)
class WindowsDataContext:
    source_root: PureWindowsPath
    backup_volume: VolumeObservation


class SourceSnapshotRunner(Protocol):
    def run(
        self,
        *,
        job_id: str,
        run_id: UUID,
        source_path: str,
        action: Callable[[PureWindowsPath], T],
    ) -> T: ...


class ExecutorWindowsJob:
    def __init__(
        self,
        *,
        coordinator: ExecutorWindowsCoordinator,
        source_snapshots: SourceSnapshotRunner,
    ) -> None:
        self._coordinator = coordinator
        self._source_snapshots = source_snapshots

    def run(
        self,
        *,
        config: DataJobConfig,
        smart_config: SmartConfig,
        run_id: UUID,
        adapter: Callable[[WindowsDataContext], T],
    ) -> ExecutorWindowsResult:
        def with_backup_volume(volume: VolumeObservation) -> T:
            return self._source_snapshots.run(
                job_id=config.id,
                run_id=run_id,
                source_path=config.source.path,
                action=lambda source_root: adapter(WindowsDataContext(source_root, volume)),
            )

        return self._coordinator.run(
            disk=config.disk,
            marker=marker_expectation(config),
            smart_config=smart_config,
            action=with_backup_volume,
        )

    def run_destination(
        self,
        *,
        config: DataJobConfig,
        smart_config: SmartConfig,
        adapter: Callable[[WindowsDataContext], T],
    ) -> ExecutorWindowsResult:
        return self._coordinator.run(
            disk=config.disk,
            marker=marker_expectation(config),
            smart_config=smart_config,
            action=lambda volume: adapter(
                WindowsDataContext(PureWindowsPath(config.source.path), volume)
            ),
        )


def marker_expectation(config: DataJobConfig) -> MarkerExpectation:
    if isinstance(config, SnapshotJobConfig):
        return MarkerExpectation(
            file=config.repository.marker_file,
            marker_uuid=config.repository.marker_uuid,
        )
    return MarkerExpectation(
        file=config.destination.marker_file,
        marker_uuid=config.destination.marker_uuid,
    )
