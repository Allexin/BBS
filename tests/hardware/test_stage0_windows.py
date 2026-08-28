import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_ROOT = PROJECT_ROOT / ".poc-work" / "stage0"

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        os.environ.get("BBS_RUN_STAGE0_HARDWARE") != "1",
        reason="set BBS_RUN_STAGE0_HARDWARE=1 for the guarded disposable-disk suite",
    ),
]


def _guarded_drive() -> str:
    drive = os.environ.get("BBS_HARDWARE_TEST_DRIVE", "").strip().upper()
    guard = os.environ.get("BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID", "").strip()
    if len(drive) != 1 or drive not in "DEFGHIJKLMNOPQRSTUVWXYZ":
        pytest.fail("BBS_HARDWARE_TEST_DRIVE must be one non-system drive letter")
    if not guard:
        pytest.fail("BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID must contain the exact preflight ID")
    return drive


def _assert_passed(path: Path) -> None:
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    assert result["status"] == "passed", result


@pytest.mark.timeout(900)
def test_vss_restic_and_offline_cycle_on_disposable_disk() -> None:
    drive = _guarded_drive()
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "poc" / "stage0" / "admin_hardware_test.ps1"),
            "-TestDrive",
            drive,
        ],
        cwd=PROJECT_ROOT,
        check=False,
        timeout=890,
    )
    assert completed.returncode == 0
    _assert_passed(RESULT_ROOT / "admin-hardware-result.json")


@pytest.mark.timeout(180)
def test_direct_storage_api_on_disposable_disk() -> None:
    drive = _guarded_drive()
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "poc" / "stage0" / "storage_api_probe.py"),
            "--drive",
            drive,
        ],
        cwd=PROJECT_ROOT,
        check=False,
        timeout=170,
    )
    assert completed.returncode == 0
    _assert_passed(RESULT_ROOT / "storage-api-result.json")
