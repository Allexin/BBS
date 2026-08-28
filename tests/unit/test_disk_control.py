from dataclasses import replace

import pytest

from backup_system.common.config import DiskConfig
from backup_system.executor.disk_control import (
    DiskCandidate,
    DiskController,
    DiskIdentityMismatchError,
    DiskStateTimeoutError,
)


def _config() -> DiskConfig:
    return DiskConfig(
        physical_serial="TEST-SERIAL",
        expected_size_bytes=1000,
        partition_guid="{partition-guid}",
        volume_guid="{volume-guid}",
        mount_point=r"C:\BackupVolumes\primary",
        repository_path_timeout_seconds=2,
    )


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeStorage:
    def __init__(self, candidate: DiskCandidate) -> None:
        self.candidate = candidate
        self.ignore_state_changes = False
        self.path_is_available = True
        self.set_calls: list[tuple[int, bool]] = []
        self.mount_calls: list[tuple[str, str]] = []

    def enumerate_disks(self) -> tuple[DiskCandidate, ...]:
        return (self.candidate,)

    def set_offline(self, disk_number: int, offline: bool) -> None:
        self.set_calls.append((disk_number, offline))
        if not self.ignore_state_changes:
            self.candidate = replace(self.candidate, offline=offline)

    def is_offline(self, disk_number: int) -> bool:
        return self.candidate.offline

    def ensure_mount_point(self, volume_guid: str, mount_point: str) -> None:
        self.mount_calls.append((volume_guid, mount_point))

    def path_available(self, path: str) -> bool:
        return self.path_is_available


def _candidate() -> DiskCandidate:
    return DiskCandidate(
        disk_number=7,
        physical_serial=" test-serial\x00 ",
        size_bytes=1000,
        partition_guid="partition-guid",
        volume_guid="volume-guid",
        offline=True,
    )


def _controller(storage: FakeStorage, clock: FakeClock | None = None) -> DiskController:
    clock = clock or FakeClock()
    return DiskController(
        storage,
        state_timeout_seconds=1,
        poll_seconds=0.1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def test_identity_is_verified_before_any_state_change() -> None:
    storage = FakeStorage(replace(_candidate(), size_bytes=999))
    with pytest.raises(DiskIdentityMismatchError):
        _controller(storage).inspect(_config())
    assert storage.set_calls == []


def test_online_mount_and_offline_reverify_identity() -> None:
    storage = FakeStorage(_candidate())
    controller = _controller(storage)
    observation = controller.inspect(_config())
    controller.bring_online(_config(), observation.verified)
    volume = controller.ensure_repository_path(_config(), observation.verified)
    controller.take_offline(_config(), observation.verified)
    assert storage.set_calls == [(7, False), (7, True)]
    assert storage.mount_calls == [("{volume-guid}", r"C:\BackupVolumes\primary")]
    assert volume.available


def test_changed_identity_blocks_offline_mutation() -> None:
    storage = FakeStorage(_candidate())
    controller = _controller(storage)
    verified = controller.inspect(_config()).verified
    storage.candidate = replace(storage.candidate, partition_guid="replacement")
    with pytest.raises(DiskIdentityMismatchError):
        controller.take_offline(_config(), verified)
    assert storage.set_calls == []


def test_changed_windows_disk_number_is_rediscovered_not_treated_as_identity() -> None:
    storage = FakeStorage(_candidate())
    controller = _controller(storage)
    verified = controller.inspect(_config()).verified
    storage.candidate = replace(storage.candidate, disk_number=9)
    controller.bring_online(_config(), verified)
    assert storage.set_calls == [(9, False)]


def test_multiple_partitions_on_same_physical_disk_do_not_make_serial_ambiguous() -> None:
    storage = FakeStorage(_candidate())
    second = replace(_candidate(), partition_guid="other", volume_guid="other")
    storage.enumerate_disks = lambda: (storage.candidate, second)  # type: ignore[method-assign]
    observation = _controller(storage).inspect(_config())
    assert observation.verified.partition_guid == "partition-guid"


def test_online_timeout_is_bounded() -> None:
    storage = FakeStorage(_candidate())
    storage.ignore_state_changes = True
    controller = _controller(storage)
    verified = controller.inspect(_config()).verified
    with pytest.raises(DiskStateTimeoutError, match="did not become False"):
        controller.bring_online(_config(), verified)
