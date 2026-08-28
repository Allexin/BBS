"""Direct Win32 storage mutations used by the executor disk controller."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from backup_system.executor.disk_control import DiskCandidate, DiskControlError

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
IOCTL_DISK_GET_DISK_ATTRIBUTES = 0x000700F0
IOCTL_DISK_SET_DISK_ATTRIBUTES = 0x0007C0F4
DISK_ATTRIBUTE_OFFLINE = 0x1


class _GetDiskAttributes(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD),
        ("Reserved1", wintypes.DWORD),
        ("Attributes", ctypes.c_ulonglong),
    ]


class _SetDiskAttributes(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD),
        ("Persist", wintypes.BOOLEAN),
        ("Reserved1", ctypes.c_ubyte * 3),
        ("Attributes", ctypes.c_ulonglong),
        ("AttributesMask", ctypes.c_ulonglong),
        ("Reserved2", wintypes.DWORD * 4),
    ]


class NativeStorageApi(Protocol):
    def disk_attributes(self, disk_number: int) -> int: ...

    def set_disk_offline(self, disk_number: int, offline: bool) -> None: ...

    def mounted_volume(self, mount_point: str) -> str | None: ...

    def set_mount_point(self, mount_point: str, volume_name: str) -> None: ...


class CtypesStorageApi:
    """Small, localized ctypes boundary for documented Windows storage APIs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 storage API is available only on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind_signatures()

    def disk_attributes(self, disk_number: int) -> int:
        attributes = _GetDiskAttributes()
        attributes.Version = ctypes.sizeof(_GetDiskAttributes)
        with self._disk_handle(disk_number) as handle:
            self._device_io(handle, IOCTL_DISK_GET_DISK_ATTRIBUTES, None, attributes)
        return int(attributes.Attributes)

    def set_disk_offline(self, disk_number: int, offline: bool) -> None:
        attributes = _SetDiskAttributes()
        attributes.Version = ctypes.sizeof(_SetDiskAttributes)
        attributes.Persist = False
        attributes.Attributes = DISK_ATTRIBUTE_OFFLINE if offline else 0
        attributes.AttributesMask = DISK_ATTRIBUTE_OFFLINE
        with self._disk_handle(disk_number) as handle:
            self._device_io(handle, IOCTL_DISK_SET_DISK_ATTRIBUTES, attributes, None)

    def mounted_volume(self, mount_point: str) -> str | None:
        buffer = ctypes.create_unicode_buffer(128)
        if self._kernel32.GetVolumeNameForVolumeMountPointW(
            _mount_argument(mount_point), buffer, len(buffer)
        ):
            return str(buffer.value)
        error = ctypes.get_last_error()
        if error in {2, 3, 21, 87}:
            return None
        raise ctypes.WinError(error, "GetVolumeNameForVolumeMountPointW failed")

    def set_mount_point(self, mount_point: str, volume_name: str) -> None:
        if not self._kernel32.SetVolumeMountPointW(
            _mount_argument(mount_point), _volume_argument(volume_name)
        ):
            raise ctypes.WinError(ctypes.get_last_error(), "SetVolumeMountPointW failed")

    def _bind_signatures(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        kernel32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
        kernel32.SetVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        kernel32.SetVolumeMountPointW.restype = wintypes.BOOL

    def _disk_handle(self, disk_number: int) -> _Handle:
        handle = self._kernel32.CreateFileW(
            rf"\\.\PhysicalDrive{disk_number}",
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error(), "CreateFileW failed")
        return _Handle(self._kernel32, int(handle))

    def _device_io(
        self,
        handle: int,
        code: int,
        input_value: ctypes.Structure | None,
        output_value: ctypes.Structure | None,
    ) -> None:
        returned = wintypes.DWORD()
        input_pointer = ctypes.byref(input_value) if input_value is not None else None
        input_size = ctypes.sizeof(input_value) if input_value is not None else 0
        output_pointer = ctypes.byref(output_value) if output_value is not None else None
        output_size = ctypes.sizeof(output_value) if output_value is not None else 0
        if not self._kernel32.DeviceIoControl(
            handle,
            code,
            input_pointer,
            input_size,
            output_pointer,
            output_size,
            ctypes.byref(returned),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error(), f"DeviceIoControl 0x{code:08x} failed")


class _Handle:
    def __init__(self, kernel32: object, value: int) -> None:
        self._kernel32 = kernel32
        self._value = value

    def __enter__(self) -> int:
        return self._value

    def __exit__(self, *_: object) -> None:
        self._kernel32.CloseHandle(self._value)  # type: ignore[attr-defined]


class Win32StorageBackend:
    """StorageBackend using direct APIs and an identity inventory provider."""

    def __init__(
        self,
        inventory: Callable[[], Sequence[DiskCandidate]],
        *,
        native: NativeStorageApi | None = None,
    ) -> None:
        self._inventory = inventory
        self._native = native or CtypesStorageApi()

    def enumerate_disks(self) -> Sequence[DiskCandidate]:
        return self._inventory()

    def set_offline(self, disk_number: int, offline: bool) -> None:
        self._native.set_disk_offline(disk_number, offline)

    def is_offline(self, disk_number: int) -> bool:
        return bool(self._native.disk_attributes(disk_number) & DISK_ATTRIBUTE_OFFLINE)

    def ensure_mount_point(self, volume_guid: str, mount_point: str) -> None:
        expected = _volume_argument(volume_guid)
        mounted = self._native.mounted_volume(mount_point)
        if mounted is not None:
            if mounted.casefold() != expected.casefold():
                raise DiskControlError("mount point belongs to a different volume")
            return
        path = Path(mount_point)
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise DiskControlError("mount point must be an empty directory")
        path.mkdir(parents=True, exist_ok=True)
        self._native.set_mount_point(mount_point, expected)
        if (actual := self._native.mounted_volume(mount_point)) is None:
            raise DiskControlError("mount point assignment could not be verified")
        if actual.casefold() != expected.casefold():
            raise DiskControlError("mounted volume identity differs from configuration")

    def path_available(self, path: str) -> bool:
        return self._native.mounted_volume(path) is not None


def _mount_argument(path: str) -> str:
    return path.rstrip("\\/") + "\\"


def _volume_argument(volume_guid: str) -> str:
    value = volume_guid.strip()
    if value.casefold().startswith("\\\\?\\volume{"):
        return _mount_argument(value)
    return "\\\\?\\Volume{" + value.strip("{}") + "}\\"
