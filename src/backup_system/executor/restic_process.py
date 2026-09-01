"""Version-pinned restic process boundary with cooperative fail-fast termination."""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Any, Literal

from backup_system.executor.cancellation import CancellationRequested, CancellationToken

SUPPORTED_RESTIC_VERSION = (0, 19)
_VERSION = re.compile(r"^restic (\d+)\.(\d+)\.(\d+)(?:\s|$)")
_DISK_FULL = "There is not enough space on the disk."
_REPOSITORY_RETRY = "returned error, retrying after"

ResticFault = Literal[
    "source_read_error",
    "repository_io_error",
    "repository_out_of_space",
    "repository_key_invalid",
    "repository_auth_mode_mismatch",
    "command_failed",
    "malformed_output",
    "unsupported_version",
]


class ResticProcessError(RuntimeError):
    def __init__(self, fault: ResticFault, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.fault = fault
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class ResticResult:
    exit_code: int
    events: tuple[Mapping[str, Any], ...]
    source_read_errors: tuple[Mapping[str, Any], ...] = ()


class ResticProcess:
    def __init__(
        self,
        executable: Path,
        cancellation: CancellationToken,
        *,
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
        terminate_timeout_seconds: float = 15.0,
    ) -> None:
        self._executable = executable
        self._cancellation = cancellation
        self._event_sink = event_sink or (lambda event: None)
        self._terminate_timeout = terminate_timeout_seconds

    def verify_version(self) -> tuple[int, int, int]:
        completed = subprocess.run(
            [str(self._executable), "version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        match = _VERSION.match(completed.stdout.strip())
        if completed.returncode != 0 or match is None:
            raise ResticProcessError("unsupported_version", "restic version is unreadable")
        version = tuple(int(part) for part in match.groups())
        if version[:2] != SUPPORTED_RESTIC_VERSION:
            raise ResticProcessError("unsupported_version", "restic major/minor is unsupported")
        return version  # type: ignore[return-value]

    def run(self, arguments: Sequence[str], *, expect_json: bool = True) -> ResticResult:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            [str(self._executable), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        assert process.stdout is not None and process.stderr is not None
        lines: queue.Queue[tuple[str, str]] = queue.Queue()
        threads = (
            Thread(target=_read_lines, args=("stdout", process.stdout, lines), daemon=True),
            Thread(target=_read_lines, args=("stderr", process.stderr, lines), daemon=True),
        )
        for thread in threads:
            thread.start()

        events: list[Mapping[str, Any]] = []
        source_read_errors: list[Mapping[str, Any]] = []
        detected: ResticFault | None = None
        try:
            while (
                process.poll() is None
                or any(thread.is_alive() for thread in threads)
                or not lines.empty()
            ):
                if self._cancellation.requested:
                    self._terminate(process)
                    raise CancellationRequested("executor cancellation was requested")
                try:
                    stream, line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                parsed = _parse_events(line)
                for event in parsed:
                    events.append(event)
                    self._event_sink(event)
                detected = next(
                    (
                        fault
                        for event in parsed or (None,)
                        if (
                            fault := _classify_fault(
                                stream, line, event, _repository_argument(arguments)
                            )
                        )
                        is not None
                    ),
                    None,
                )
                if detected == "source_read_error":
                    source_read_errors.extend(
                        event
                        for event in parsed
                        if event.get("message_type") == "error"
                        and event.get("during") == "archival"
                    )
                    detected = None
                if detected is not None:
                    self._terminate(process)
                    raise ResticProcessError(
                        detected,
                        f"restic failed: {detected}; {_fault_diagnostic(line, parsed)}",
                        exit_code=process.returncode,
                    )
                if expect_json and stream == "stdout" and line and not parsed:
                    self._terminate(process)
                    raise ResticProcessError("malformed_output", "restic emitted malformed JSON")
            return_code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=2)
        if return_code == 3 and source_read_errors:
            return ResticResult(return_code, tuple(events), tuple(source_read_errors))
        if return_code != 0:
            raise ResticProcessError(
                "command_failed",
                f"restic {_operation_name(arguments)} failed with exit code {return_code}",
                exit_code=return_code,
            )
        return ResticResult(return_code, tuple(events), tuple(source_read_errors))

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        try:
            process.wait(timeout=self._terminate_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _read_lines(stream_name: str, stream: Any, output: queue.Queue[tuple[str, str]]) -> None:
    for line in stream:
        output.put((stream_name, line.rstrip("\r\n")))


def _parse_events(line: str) -> tuple[Mapping[str, Any], ...]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return ()
    if isinstance(value, dict):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return tuple(value)
    return ()


def _fault_diagnostic(line: str, events: tuple[Mapping[str, Any], ...]) -> str:
    value = json.dumps(events[0], ensure_ascii=True, separators=(",", ":")) if events else line
    return value[:4000]


def _classify_fault(
    stream: str,
    line: str,
    event: Mapping[str, Any] | None,
    repository: str | None = None,
) -> ResticFault | None:
    if event is not None and event.get("message_type") == "error":
        if event.get("during") == "archival":
            return "source_read_error"
        return "repository_io_error"
    if stream != "stderr":
        return None
    lowered = line.casefold()
    if _DISK_FULL in line:
        return "repository_out_of_space"
    if _REPOSITORY_RETRY in line and repository is not None and repository.casefold() in lowered:
        return "repository_io_error"
    if "wrong password or no key found" in lowered:
        return "repository_key_invalid"
    if "repository is already initialized" in lowered:
        return "repository_auth_mode_mismatch"
    return None


def _repository_argument(arguments: Sequence[str]) -> str | None:
    for flag in ("--repository", "--repo", "-r"):
        try:
            return arguments[arguments.index(flag) + 1]
        except (ValueError, IndexError):
            continue
    return None


def _operation_name(arguments: Sequence[str]) -> str:
    known = {"backup", "check", "forget", "init", "prune", "restore", "snapshots"}
    return next((value for value in arguments if value in known), "command")
