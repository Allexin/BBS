"""Elevated direct Win32 storage lifecycle probe for the selected test disk."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_PATH = PROJECT_ROOT / ".poc-work" / "stage0" / "admin-preflight.json"
RESULT_PATH = PROJECT_ROOT / ".poc-work" / "stage0" / "storage-api-result.json"
MOUNT_PATH = Path(os.environ.get("SystemDrive", "C:")) / "BBSStage0ApiMount"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
IOCTL_DISK_GET_DISK_ATTRIBUTES = 0x000700F0
IOCTL_DISK_SET_DISK_ATTRIBUTES = 0x0007C0F4
DISK_ATTRIBUTE_OFFLINE = 0x1


class GetDiskAttributes(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD),
        ("Reserved1", wintypes.DWORD),
        ("Attributes", ctypes.c_ulonglong),
    ]


class SetDiskAttributes(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD),
        ("Persist", wintypes.BOOLEAN),
        ("Reserved1", ctypes.c_ubyte * 3),
        ("Attributes", ctypes.c_ulonglong),
        ("AttributesMask", ctypes.c_ulonglong),
        ("Reserved2", wintypes.DWORD * 4),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

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
kernel32.DeleteVolumeMountPointW.argtypes = [wintypes.LPCWSTR]
kernel32.DeleteVolumeMountPointW.restype = wintypes.BOOL


def win_error(message: str) -> OSError:
    return ctypes.WinError(ctypes.get_last_error(), message)


def open_device(path: str, write: bool = False) -> int:
    access = GENERIC_READ | (GENERIC_WRITE if write else 0)
    handle = kernel32.CreateFileW(
        path,
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise win_error(f"CreateFileW failed for {path}")
    return int(handle)


def device_io(handle: int, code: int, input_value: object | None, output_value: object | None) -> None:
    returned = wintypes.DWORD()
    input_pointer = ctypes.byref(input_value) if input_value is not None else None
    input_size = ctypes.sizeof(input_value) if input_value is not None else 0
    output_pointer = ctypes.byref(output_value) if output_value is not None else None
    output_size = ctypes.sizeof(output_value) if output_value is not None else 0
    if not kernel32.DeviceIoControl(
        handle,
        code,
        input_pointer,
        input_size,
        output_pointer,
        output_size,
        ctypes.byref(returned),
        None,
    ):
        raise win_error(f"DeviceIoControl 0x{code:08x} failed")


def disk_number_for_drive(drive: str) -> int:
    handle = open_device(rf"\\.\{drive}:")
    try:
        buffer = ctypes.create_string_buffer(1024)
        device_io(handle, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, buffer)
        extent_count = int.from_bytes(buffer.raw[0:4], "little")
        if extent_count != 1:
            raise RuntimeError(f"expected one disk extent, got {extent_count}")
        return int.from_bytes(buffer.raw[8:12], "little")
    finally:
        kernel32.CloseHandle(handle)


def get_attributes(disk_number: int) -> int:
    handle = open_device(rf"\\.\PhysicalDrive{disk_number}", write=True)
    try:
        attributes = GetDiskAttributes()
        attributes.Version = ctypes.sizeof(GetDiskAttributes)
        device_io(handle, IOCTL_DISK_GET_DISK_ATTRIBUTES, None, attributes)
        return int(attributes.Attributes)
    finally:
        kernel32.CloseHandle(handle)


def set_offline(disk_number: int, offline: bool) -> None:
    handle = open_device(rf"\\.\PhysicalDrive{disk_number}", write=True)
    try:
        attributes = SetDiskAttributes()
        attributes.Version = ctypes.sizeof(SetDiskAttributes)
        attributes.Persist = False
        attributes.Attributes = DISK_ATTRIBUTE_OFFLINE if offline else 0
        attributes.AttributesMask = DISK_ATTRIBUTE_OFFLINE
        device_io(handle, IOCTL_DISK_SET_DISK_ATTRIBUTES, attributes, None)
    finally:
        kernel32.CloseHandle(handle)


def wait_for_attribute(disk_number: int, offline: bool, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        actual = bool(get_attributes(disk_number) & DISK_ATTRIBUTE_OFFLINE)
        if actual == offline:
            return
        time.sleep(0.5)
    raise RuntimeError(f"disk offline state did not become {offline}")


def volume_guid(drive: str) -> str:
    buffer = ctypes.create_unicode_buffer(128)
    if not kernel32.GetVolumeNameForVolumeMountPointW(f"{drive}:\\", buffer, len(buffer)):
        raise win_error("GetVolumeNameForVolumeMountPointW failed")
    return buffer.value


def write_result(value: dict[str, object]) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(value, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive", default="D", choices=list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    args = parser.parse_args()
    drive = args.drive.upper()
    if not shell32.IsUserAnAdmin():
        raise RuntimeError("run from an elevated PowerShell")

    preflight = json.loads(PREFLIGHT_PATH.read_text(encoding="utf-8-sig"))
    matches = [
        disk
        for disk in preflight["disks"]
        if drive in [part["drive_letter"] for part in disk["partitions"]]
    ]
    if len(matches) != 1:
        raise RuntimeError("preflight does not identify exactly one selected disk")
    expected = matches[0]
    expected_id = str(expected["unique_id"]).strip()
    if os.environ.get("BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID") != expected_id:
        raise RuntimeError("hardware test disk guard mismatch")
    if expected["is_boot"] or expected["is_system"]:
        raise RuntimeError("boot and system disks are forbidden")

    disk_number = disk_number_for_drive(drive)
    if disk_number != expected["number"]:
        raise RuntimeError("direct volume extent does not match preflight disk number")
    if get_attributes(disk_number) & DISK_ATTRIBUTE_OFFLINE:
        raise RuntimeError("test disk must initially be online")

    guid = volume_guid(drive)
    marker = Path(f"{drive}:\\bbs-stage0-poc\\api-marker.txt")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("direct storage api probe", encoding="ascii")
    mount_created = False
    offline_observed = False
    online_restored = False
    try:
        set_offline(disk_number, True)
        wait_for_attribute(disk_number, True)
        offline_observed = True
    finally:
        set_offline(disk_number, False)
        wait_for_attribute(disk_number, False)
        online_restored = True

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.5)
    if not marker.exists():
        raise RuntimeError("drive mount did not return after online")

    if MOUNT_PATH.exists():
        if any(MOUNT_PATH.iterdir()):
            raise RuntimeError("temporary mount directory is not empty")
        MOUNT_PATH.rmdir()
    MOUNT_PATH.mkdir(parents=True)
    mount_argument = str(MOUNT_PATH) + "\\"
    try:
        if not kernel32.SetVolumeMountPointW(mount_argument, guid):
            raise win_error("SetVolumeMountPointW failed")
        mount_created = True
        mounted_marker = MOUNT_PATH / marker.name
        if not mounted_marker.is_file() or mounted_marker.read_text(encoding="ascii") != "direct storage api probe":
            raise RuntimeError("mounted folder does not expose the expected volume")
    finally:
        if mount_created and not kernel32.DeleteVolumeMountPointW(mount_argument):
            raise win_error("DeleteVolumeMountPointW failed")
        if MOUNT_PATH.exists():
            MOUNT_PATH.rmdir()
        if marker.exists():
            marker.unlink()

    write_result(
        {
            "schema_version": 1,
            "status": "passed",
            "drive": drive,
            "disk_number": disk_number,
            "volume_extent_match": True,
            "direct_ioctl_offline_observed": offline_observed,
            "direct_ioctl_online_restored": online_restored,
            "temporary_mount_point_verified": True,
        }
    )
    print(f"Result saved to: {RESULT_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        write_result({"schema_version": 1, "status": "failed", "error": str(error)})
        print(f"PoC failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
