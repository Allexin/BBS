"""Elevated, guarded native VSS probe restricted to one disposable test drive."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from backup_system.executor.native_vss import NativeVssBackend
from backup_system.executor.vss import VssSnapshot
from backup_system.executor.vss_intent import OwnedVssSnapshotManager, VssIntentStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORK_ROOT = PROJECT_ROOT / ".poc-work" / "stage0"
PREFLIGHT_PATH = WORK_ROOT / "admin-preflight.json"
RESULT_PATH = WORK_ROOT / "native-vss-result.json"
TEST_DIRECTORY_NAME = "bbs-stage0-native-vss"


def _volume_guid(drive: str) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetVolumeNameForVolumeMountPointW
    function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    function.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(128)
    if not function(f"{drive}:\\", buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error(), "volume GUID lookup failed")
    return str(buffer.value)


def _verify_guard(drive: str) -> None:
    if not ctypes.WinDLL("shell32").IsUserAnAdmin():
        raise RuntimeError("run from an elevated PowerShell")
    preflight = json.loads(PREFLIGHT_PATH.read_text(encoding="utf-8-sig"))
    matches = [
        disk
        for disk in preflight["disks"]
        if drive in [partition["drive_letter"] for partition in disk["partitions"]]
    ]
    if len(matches) != 1:
        raise RuntimeError("preflight does not identify exactly one selected disk")
    disk = matches[0]
    expected_id = str(disk["unique_id"]).strip()
    if os.environ.get("BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID", "").strip() != expected_id:
        raise RuntimeError("hardware test disk guard mismatch")
    if disk["is_boot"] or disk["is_system"]:
        raise RuntimeError("boot and system disks are forbidden")


def _write_result(payload: dict[str, object]) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive", required=True, choices=list("DEFGHIJKLMNOPQRSTUVWXYZ"))
    arguments = parser.parse_args()
    drive = arguments.drive.upper()
    _verify_guard(drive)

    test_directory = Path(f"{drive}:\\{TEST_DIRECTORY_NAME}")
    control_file = test_directory / "control.bin"
    if test_directory.exists():
        raise RuntimeError("native VSS test directory already exists")
    test_directory.mkdir()
    expected = os.urandom(4096)
    control_file.write_bytes(expected)
    intent_directory = WORK_ROOT / "native-vss-intents"
    backend = NativeVssBackend()
    manager = OwnedVssSnapshotManager(backend, VssIntentStore(intent_directory))
    observed = False
    try:
        def verify(snapshot: VssSnapshot) -> None:
            nonlocal observed
            shadow_file = Path(snapshot.shadow_device_path) / TEST_DIRECTORY_NAME / "control.bin"
            if shadow_file.read_bytes() != expected:
                raise RuntimeError("native VSS control bytes differ")
            observed = True

        manager.run(
            job_id="stage0-native-vss",
            run_id=uuid4(),
            volume_guid=_volume_guid(drive),
            action=verify,
        )
    finally:
        control_file.unlink(missing_ok=True)
        if test_directory.exists():
            test_directory.rmdir()

    _write_result(
        {
            "schema_version": 1,
            "status": "passed",
            "drive": drive,
            "shadow_bytes_verified": observed,
            "owned_intent_cleared": not any(intent_directory.glob("*.json")),
        }
    )
    print(f"Result saved to: {RESULT_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        _write_result({"schema_version": 1, "status": "failed", "error": str(error)})
        print(f"Native VSS probe failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
