"""Detach an isolated VHD during restic backup and classify repository I/O failure."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time


RETRY_PREFIX = "returned error, retrying after"


def read_lines(name: str, stream: object, events: queue.Queue[tuple[str, str]], saved: list[str]) -> None:
    for line in stream:  # type: ignore[union-attr]
        text = line.rstrip("\r\n")
        saved.append(text)
        events.put((name, text))


def detach_vhd(vhd_path: Path, script_path: Path) -> None:
    script_path.write_text(
        f'select vdisk file="{vhd_path}"\n' 'detach vdisk\n',
        encoding="ascii",
    )
    completed = subprocess.run(
        ["diskpart.exe", "/s", str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 or "successfully detached" not in output.lower():
        raise RuntimeError(f"diskpart detach failed: {output.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restic", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--vhd", required=True, type=Path)
    parser.add_argument("--diskpart-script", required=True, type=Path)
    parser.add_argument("--debug-output", required=True, type=Path)
    args = parser.parse_args()

    command = [
        args.restic,
        "--repo",
        args.repository,
        "--insecure-no-password",
        "--no-cache",
        "backup",
        "--json",
        args.source,
    ]
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
    events: queue.Queue[tuple[str, str]] = queue.Queue()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    threads = [
        threading.Thread(target=read_lines, args=("stdout", process.stdout, events, stdout_lines), daemon=True),
        threading.Thread(target=read_lines, args=("stderr", process.stderr, events, stderr_lines), daemon=True),
    ]
    for thread in threads:
        thread.start()

    started = time.monotonic()
    detached_at: float | None = None
    diagnostic_at: float | None = None
    diagnostic_stream: str | None = None
    classification_kind: str | None = None
    structured_errors = 0
    interrupt_sent = False
    deadline = started + 60
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None and events.empty():
                break
            try:
                stream_name, line = events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
            message_type = event.get("message_type") if isinstance(event, dict) else None
            if message_type in {"error", "exit_error"}:
                structured_errors += 1
            if detached_at is None and isinstance(event, dict) and event.get("message_type") == "status":
                if int(event.get("bytes_done", 0)) > 0:
                    detach_vhd(args.vhd, args.diskpart_script)
                    detached_at = time.monotonic()
                    continue
            if detached_at is not None and message_type in {"error", "exit_error"}:
                diagnostic_at = time.monotonic()
                diagnostic_stream = stream_name
                classification_kind = f"json_{message_type}"
                if message_type == "error" and process.poll() is None:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                    interrupt_sent = True
                break
            if detached_at is not None and RETRY_PREFIX in line and args.repository.lower() in line.lower():
                diagnostic_at = time.monotonic()
                diagnostic_stream = stream_name
                classification_kind = "pinned_retry_diagnostic"
                process.send_signal(signal.CTRL_BREAK_EVENT)
                interrupt_sent = True
                break

        if detached_at is None:
            raise RuntimeError("backup produced no progress before timeout or exit")
        if diagnostic_at is None:
            args.debug_output.parent.mkdir(parents=True, exist_ok=True)
            args.debug_output.write_text(
                json.dumps(
                    {
                        "process_return_code": process.poll(),
                        "detached": True,
                        "structured_error_events": structured_errors,
                        "stdout_tail": stdout_lines[-30:],
                        "stderr_tail": stderr_lines[-30:],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            raise RuntimeError("no classifiable repository error was observed after detach")
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("restic ignored cooperative repository I/O interruption") from error
        if process.returncode == 0:
            raise RuntimeError("restic reported a repository fault but exited successfully")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=2)

    print(
        json.dumps(
            {
                "status": "passed",
                "vhd_detached_after_progress": True,
                "diagnostic_stream": diagnostic_stream,
                "classification_kind": classification_kind,
                "structured_error_events_before_interrupt": structured_errors,
                "cooperative_interrupt_sent": interrupt_sent,
                "restic_exit_code": process.returncode,
                "seconds_from_detach_to_classification": round(diagnostic_at - detached_at, 3),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"fault probe failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
