from pathlib import Path

import pytest

from backup_system.executor.disk_control import DiskControlError
from backup_system.executor.win32_storage import DISK_ATTRIBUTE_OFFLINE, Win32StorageBackend


class FakeNative:
    def __init__(self) -> None:
        self.attributes = DISK_ATTRIBUTE_OFFLINE
        self.mounts: dict[str, str] = {}
        self.state_calls: list[tuple[int, bool]] = []

    def disk_attributes(self, disk_number: int) -> int:
        return self.attributes

    def set_disk_offline(self, disk_number: int, offline: bool) -> None:
        self.state_calls.append((disk_number, offline))
        self.attributes = DISK_ATTRIBUTE_OFFLINE if offline else 0

    def mounted_volume(self, mount_point: str) -> str | None:
        return self.mounts.get(mount_point)

    def set_mount_point(self, mount_point: str, volume_name: str) -> None:
        self.mounts[mount_point] = volume_name


def test_native_offline_attribute_is_read_and_changed() -> None:
    native = FakeNative()
    backend = Win32StorageBackend(lambda: (), native=native)
    assert backend.is_offline(7)
    backend.set_offline(7, False)
    assert not backend.is_offline(7)
    assert native.state_calls == [(7, False)]


def test_mount_point_is_assigned_and_verified(tmp_path: Path) -> None:
    native = FakeNative()
    mount = str(tmp_path / "repository")
    backend = Win32StorageBackend(lambda: (), native=native)
    backend.ensure_mount_point("volume-guid", mount)
    backend.ensure_mount_point("volume-guid", mount)
    assert native.mounts == {mount: "\\\\?\\Volume{volume-guid}\\"}
    assert backend.path_available(mount)


def test_existing_foreign_mount_is_rejected_before_mutation(tmp_path: Path) -> None:
    native = FakeNative()
    mount = str(tmp_path / "repository")
    native.mounts[mount] = "\\\\?\\Volume{foreign}\\"
    backend = Win32StorageBackend(lambda: (), native=native)
    with pytest.raises(DiskControlError, match="different volume"):
        backend.ensure_mount_point("expected", mount)
    assert native.mounts[mount] == "\\\\?\\Volume{foreign}\\"


def test_nonempty_mount_directory_is_rejected(tmp_path: Path) -> None:
    native = FakeNative()
    mount = tmp_path / "repository"
    mount.mkdir()
    (mount / "unexpected.txt").write_text("data", encoding="utf-8")
    backend = Win32StorageBackend(lambda: (), native=native)
    with pytest.raises(DiskControlError, match="empty directory"):
        backend.ensure_mount_point("expected", str(mount))
    assert native.mounts == {}
