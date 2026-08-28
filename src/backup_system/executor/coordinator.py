"""Executor Windows preflight orchestration before stage-specific data adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, TypeVar

from backup_system.common.config import DiskConfig, SmartConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.disk_control import VolumeObservation
from backup_system.executor.lifecycle import (
    ExecutorDiskLifecycle,
    LifecycleSuccess,
    MarkerExpectation,
)
from backup_system.executor.smart_preflight import SmartPreflightObservation

T = TypeVar("T")


class SmartCollector(Protocol):
    def collect(self, config: SmartConfig) -> Sequence[SmartPreflightObservation]: ...


@dataclass(frozen=True, slots=True)
class ExecutorWindowsResult:
    value: object
    smart: tuple[SmartPreflightObservation, ...]
    disk_offline_confirmed: bool


class ExecutorWindowsCoordinator:
    def __init__(
        self,
        *,
        lock_factory: Callable[[], AbstractContextManager[object]],
        disk_lifecycle: ExecutorDiskLifecycle,
        smart: SmartCollector,
        smart_sink: Callable[[tuple[SmartPreflightObservation, ...]], None],
        cancellation: CancellationToken,
    ) -> None:
        self._lock_factory = lock_factory
        self._disk_lifecycle = disk_lifecycle
        self._smart = smart
        self._smart_sink = smart_sink
        self._cancellation = cancellation

    def run(
        self,
        *,
        disk: DiskConfig,
        marker: MarkerExpectation,
        smart_config: SmartConfig,
        action: Callable[[VolumeObservation], T],
    ) -> ExecutorWindowsResult:
        observations: tuple[SmartPreflightObservation, ...] = ()

        def after_mount(volume: VolumeObservation) -> T:
            nonlocal observations
            self._cancellation.raise_if_requested()
            observations = tuple(self._smart.collect(smart_config))
            self._smart_sink(observations)
            self._cancellation.raise_if_requested()
            value = action(volume)
            self._cancellation.raise_if_requested()
            return value

        self._cancellation.raise_if_requested()
        with self._lock_factory():
            self._cancellation.raise_if_requested()
            lifecycle_result = self._disk_lifecycle.run(disk, marker, after_mount)
        return ExecutorWindowsResult(
            value=lifecycle_result.value,
            smart=observations,
            disk_offline_confirmed=lifecycle_result.disk_offline_confirmed,
        )

    def recover(
        self,
        *,
        disk: DiskConfig,
        owned_vss_cleanup: Callable[[], None],
    ) -> LifecycleSuccess:
        with self._lock_factory():
            return self._disk_lifecycle.recover(
                disk,
                pre_offline_cleanup=owned_vss_cleanup,
            )
