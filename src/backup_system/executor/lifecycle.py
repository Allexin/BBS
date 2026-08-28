"""Executor-owned disk lifecycle with mandatory cleanup and marker verification."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Protocol, TypeVar
from uuid import UUID

from backup_system.common.config import DiskConfig
from backup_system.executor.disk_control import (
    DiskObservation,
    VerifiedDisk,
    VolumeObservation,
)

T = TypeVar("T")
_MAX_MARKER_BYTES = 64 * 1024


class MarkerVerificationError(RuntimeError):
    pass


class LifecycleCleanupError(RuntimeError):
    def __init__(self, message: str, *, primary_error: BaseException | None) -> None:
        super().__init__(message)
        self.primary_error = primary_error


@dataclass(frozen=True, slots=True)
class MarkerExpectation:
    file: str
    marker_uuid: UUID


@dataclass(frozen=True, slots=True)
class LifecycleSuccess:
    value: object
    disk_offline_confirmed: bool = True


class DiskControl(Protocol):
    def inspect(self, config: DiskConfig) -> DiskObservation: ...

    def bring_online(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None: ...

    def ensure_repository_path(
        self, config: DiskConfig, verified_disk: VerifiedDisk
    ) -> VolumeObservation: ...

    def take_offline(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None: ...


class ExecutorDiskLifecycle:
    def __init__(
        self,
        control: DiskControl,
        *,
        marker_verifier: Callable[[MarkerExpectation], None] | None = None,
    ) -> None:
        self._control = control
        self._marker_verifier = marker_verifier or verify_marker

    def run(
        self,
        config: DiskConfig,
        marker: MarkerExpectation,
        action: Callable[[VolumeObservation], T],
    ) -> LifecycleSuccess:
        _require_marker_under_mount(config.mount_point, marker.file)
        observation = self._control.inspect(config)
        verified = observation.verified
        primary_error: BaseException | None = None
        try:
            self._control.bring_online(config, verified)
            volume = self._control.ensure_repository_path(config, verified)
            self._marker_verifier(marker)
            return LifecycleSuccess(action(volume))
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                self._control.take_offline(config, verified)
            except BaseException as cleanup_error:
                raise LifecycleCleanupError(
                    "backup disk offline could not be confirmed",
                    primary_error=primary_error,
                ) from cleanup_error

    def recover(self, config: DiskConfig) -> LifecycleSuccess:
        observation = self._control.inspect(config)
        try:
            self._control.take_offline(config, observation.verified)
        except BaseException as cleanup_error:
            raise LifecycleCleanupError(
                "backup disk recovery did not confirm offline",
                primary_error=None,
            ) from cleanup_error
        return LifecycleSuccess(None)


def verify_marker(expectation: MarkerExpectation) -> None:
    path = Path(expectation.file)
    if path.is_symlink() or not path.is_file():
        raise MarkerVerificationError("backup volume marker is missing or unsafe")
    if path.stat().st_size > _MAX_MARKER_BYTES:
        raise MarkerVerificationError("backup volume marker is too large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        actual = UUID(str(payload["marker_uuid"]))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise MarkerVerificationError("backup volume marker is invalid") from error
    if actual != expectation.marker_uuid:
        raise MarkerVerificationError("backup volume marker identity does not match")


def _require_marker_under_mount(mount_point: str, marker_file: str) -> None:
    mount = PureWindowsPath(mount_point)
    marker = PureWindowsPath(marker_file)
    mount_parts = tuple(part.casefold() for part in mount.parts)
    marker_parts = tuple(part.casefold() for part in marker.parts)
    if marker_parts[: len(mount_parts)] != mount_parts or marker == mount:
        raise MarkerVerificationError("backup marker must be inside configured mount point")
