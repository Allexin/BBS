"""Identity-first physical disk lifecycle with a replaceable Windows backend."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from backup_system.common.config import DiskConfig


class DiskControlError(RuntimeError):
    """A disk lifecycle operation could not be completed safely."""


class DiskNotFoundError(DiskControlError):
    pass


class DiskIdentityMismatchError(DiskControlError):
    pass


class DiskStateTimeoutError(DiskControlError):
    pass


@dataclass(frozen=True, slots=True)
class DiskCandidate:
    disk_number: int
    physical_serial: str
    size_bytes: int
    partition_guid: str
    volume_guid: str
    offline: bool
    is_boot: bool = False
    is_system: bool = False


@dataclass(frozen=True, slots=True)
class VerifiedDisk:
    physical_serial: str
    size_bytes: int
    partition_guid: str
    volume_guid: str


@dataclass(frozen=True, slots=True)
class DiskObservation:
    verified: VerifiedDisk
    offline: bool


@dataclass(frozen=True, slots=True)
class VolumeObservation:
    volume_guid: str
    mount_point: str
    available: bool


class StorageBackend(Protocol):
    def enumerate_disks(self) -> Sequence[DiskCandidate]: ...

    def set_offline(self, disk_number: int, offline: bool) -> None: ...

    def is_offline(self, disk_number: int) -> bool: ...

    def ensure_mount_point(self, volume_guid: str, mount_point: str) -> None: ...

    def path_available(self, path: str) -> bool: ...


class DiskController:
    def __init__(
        self,
        backend: StorageBackend,
        *,
        state_timeout_seconds: float = 30,
        poll_seconds: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if state_timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("disk state timeout and poll interval must be positive")
        self._backend = backend
        self._state_timeout = state_timeout_seconds
        self._poll_seconds = poll_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    def inspect(self, config: DiskConfig) -> DiskObservation:
        candidate = self._locate(config)
        return DiskObservation(self._verified(candidate), candidate.offline)

    def bring_online(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        candidate = self._reverify(config, verified_disk)
        if candidate.offline:
            self._backend.set_offline(candidate.disk_number, False)
        self._wait_for_state(candidate.disk_number, offline=False)

    def ensure_repository_path(
        self, config: DiskConfig, verified_disk: VerifiedDisk
    ) -> VolumeObservation:
        candidate = self._reverify(config, verified_disk)
        if candidate.offline:
            raise DiskControlError("repository path cannot be prepared while disk is offline")
        self._backend.ensure_mount_point(config.volume_guid, config.mount_point)
        deadline = self._monotonic() + config.repository_path_timeout_seconds
        while self._monotonic() < deadline:
            if self._backend.path_available(config.mount_point):
                return VolumeObservation(config.volume_guid, config.mount_point, True)
            self._sleep(self._poll_seconds)
        raise DiskStateTimeoutError("repository mount point did not become available")

    def take_offline(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        candidate = self._reverify(config, verified_disk)
        if not candidate.offline:
            self._backend.set_offline(candidate.disk_number, True)
        self._wait_for_state(candidate.disk_number, offline=True)

    def _reverify(self, config: DiskConfig, verified_disk: VerifiedDisk) -> DiskCandidate:
        candidate = self._locate(config)
        if self._verified(candidate) != verified_disk:
            raise DiskIdentityMismatchError("disk identity changed after verification")
        return candidate

    def _locate(self, config: DiskConfig) -> DiskCandidate:
        serial_matches = [
            item
            for item in self._backend.enumerate_disks()
            if _normalize(item.physical_serial) == _normalize(config.physical_serial)
        ]
        if not serial_matches:
            raise DiskNotFoundError("configured physical disk was not found")
        if len({item.disk_number for item in serial_matches}) != 1:
            raise DiskIdentityMismatchError("physical disk serial is not unique")
        identity_matches = [
            item
            for item in serial_matches
            if item.size_bytes == config.expected_size_bytes
            and _normalize_guid(item.partition_guid) == _normalize_guid(config.partition_guid)
            and _normalize_guid(item.volume_guid) == _normalize_guid(config.volume_guid)
        ]
        if len(identity_matches) != 1:
            raise DiskIdentityMismatchError("configured disk identity does not match hardware")
        candidate = identity_matches[0]
        if candidate.is_boot or candidate.is_system:
            raise DiskIdentityMismatchError("boot and system disks are forbidden")
        return candidate

    @staticmethod
    def _verified(candidate: DiskCandidate) -> VerifiedDisk:
        return VerifiedDisk(
            _normalize(candidate.physical_serial),
            candidate.size_bytes,
            _normalize_guid(candidate.partition_guid),
            _normalize_guid(candidate.volume_guid),
        )

    def _wait_for_state(self, disk_number: int, *, offline: bool) -> None:
        deadline = self._monotonic() + self._state_timeout
        while self._monotonic() < deadline:
            if self._backend.is_offline(disk_number) is offline:
                return
            self._sleep(self._poll_seconds)
        raise DiskStateTimeoutError(f"disk offline state did not become {offline}")


def _normalize(value: str) -> str:
    return value.replace("\x00", "").strip().casefold()


def _normalize_guid(value: str) -> str:
    return _normalize(value).strip("{}")
