"""Interrupt restic at the first version-pinned Windows disk-full diagnostic."""

from __future__ import annotations

import argparse
import json
import queue
import signal
import subprocess
import sys
import threading
import time


DISK_FULL_TEXT = "There is not enough space on the disk."


def read_lines(name: str, stream: object, events: queue.Queue[tuple[str, str]], saved: list[str]) -> None:
    for line in stream:  # type: ignore[union-attr]
        text = line.rstrip("\r\n")
        saved.append(text)
        events.put((name, text))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restic", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source", required=True)
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
    classified = False
    interrupted = False
    deadline = started + 60
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None and events.empty():
                break
            try:
                stream_name, line = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if stream_name == "stderr" and DISK_FULL_TEXT in line:
                classified = True
                process.send_signal(signal.CTRL_BREAK_EVENT)
                interrupted = True
                break
        if not classified:
            raise RuntimeError("no pinned disk-full diagnostic was observed within 60 seconds")
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("restic ignored cooperative disk-full interruption") from error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=2)

    json_errors = 0
    for line in [*stdout_lines, *stderr_lines]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("message_type") == "error":
            json_errors += 1
    print(
        json.dumps(
            {
                "status": "passed",
                "diagnostic_stream": "stderr",
                "pinned_diagnostic_matched": classified,
                "structured_error_events_before_interrupt": json_errors,
                "cooperative_interrupt_sent": interrupted,
                "restic_exit_code": process.returncode,
                "seconds_to_classification": round(time.monotonic() - started, 3),
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
