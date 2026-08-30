import ctypes
import os
import subprocess
import sys
import time

import pytest

from backup_system.manager.win32_job import KillOnCloseJob


def _process_is_running(process_id: int) -> bool:
    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, process_id)
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


@pytest.mark.integration
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_closing_job_terminates_contained_process() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = KillOnCloseJob()
        job.assign(process.pid)
        job.close()
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


@pytest.mark.integration
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_closing_job_terminates_inherited_descendant(tmp_path) -> None:
    release = tmp_path / "release"
    child_pid_file = tmp_path / "child.pid"
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        f"go=pathlib.Path({str(release)!r});pid=pathlib.Path({str(child_pid_file)!r});"
        "\nwhile not go.exists(): time.sleep(.01)\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "pid.write_text(str(child.pid));time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    child_pid = 0
    try:
        job = KillOnCloseJob()
        job.assign(parent.pid)
        release.touch()
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        assert _process_is_running(child_pid)
        job.close()
        deadline = time.monotonic() + 5
        while (
            _process_is_running(parent.pid) or _process_is_running(child_pid)
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _process_is_running(parent.pid)
        assert not _process_is_running(child_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait()
