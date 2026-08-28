"""Test structured source errors and cooperative restic interruption."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

from restic_local import PROJECT_ROOT, find_restic, load_lock, run


WORK_ROOT = PROJECT_ROOT / ".poc-work" / "stage0" / "restic-fail-fast"
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def open_exclusively(path: Path) -> int:
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError()
    return int(handle)


def collect_lines(
    stream: object,
    output: list[str],
    event_queue: queue.Queue[str] | None = None,
) -> None:
    for line in stream:  # type: ignore[union-attr]
        text = line.rstrip("\r\n")
        output.append(text)
        if event_queue is not None:
            event_queue.put(text)


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("this PoC is Windows-only")
    restic = find_restic(load_lock())
    expected_parent = PROJECT_ROOT / ".poc-work" / "stage0"
    if WORK_ROOT.parent != expected_parent:
        raise RuntimeError("unsafe PoC workspace path")
    if WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)

    source = WORK_ROOT / "source"
    repository = WORK_ROOT / "repository"
    source.mkdir(parents=True)
    locked_path = source / "00-locked.bin"
    locked_path.write_bytes(b"sharing violation fixture\n")
    with (source / "zz-filler.bin").open("wb") as filler:
        for _ in range(64):
            filler.write(os.urandom(1024 * 1024))
    run(restic, repository, "init", "--repository-version", "stable")

    command = [
        str(restic),
        "--repo",
        str(repository),
        "--insecure-no-password",
        "--no-cache",
        "backup",
        "--json",
        str(source),
    ]
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    event_queue: queue.Queue[str] = queue.Queue()
    process: subprocess.Popen[str] | None = None
    error_event: dict[str, object] | None = None
    interrupt_sent = False
    started = time.monotonic()
    handle = open_exclusively(locked_path)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=collect_lines,
            args=(process.stdout, stdout_lines, event_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=collect_lines,
            args=(process.stderr, stderr_lines, event_queue),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        deadline = started + 30
        while time.monotonic() < deadline:
            if process.poll() is not None and event_queue.empty():
                break
            try:
                line = event_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("message_type") == "error":
                error_event = event
                process.send_signal(signal.CTRL_BREAK_EVENT)
                interrupt_sent = True
                break

        if error_event is None:
            raise RuntimeError("no structured restic error was emitted within 30 seconds")
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("restic ignored cooperative interruption") from error
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    run(restic, repository, "check", "--read-data")
    snapshots_value = json.loads(run(restic, repository, "snapshots", "--json").stdout)
    snapshots = snapshots_value or []
    if snapshots:
        raise RuntimeError("interrupted backup unexpectedly published a snapshot")

    result = {
        "status": "passed",
        "structured_error": True,
        "error_during": error_event.get("during"),
        "cooperative_interrupt_sent": interrupt_sent,
        "restic_exit_code": process.returncode if process else None,
        "seconds_until_repository_verified": round(time.monotonic() - started, 3),
        "repository_check_after_interrupt": "passed",
        "snapshots_after_interrupt": len(snapshots),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"PoC failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
