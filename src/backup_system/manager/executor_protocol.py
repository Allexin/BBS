"""Bounded executor argv and incremental UTF-8 JSON Lines event decoding."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from backup_system.common.config import validate_job_id
from backup_system.common.events import (
    KnownExecutorEvent,
    UnknownExecutorEvent,
    parse_executor_event,
)

MAX_EVENT_LINE_BYTES = 1024 * 1024


class ExecutorProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutorInvocation:
    python_executable: Path
    operation: str
    run_id: UUID
    job_id: str
    mode: str | None = None
    request_file: Path | None = None

    def argv(self) -> tuple[str, ...]:
        if not self.python_executable.is_absolute():
            raise ValueError("Python executable path must be absolute")
        if self.operation not in {
            "run",
            "check",
            "prune",
            "restore",
            "resolve-restore",
            "restore-test",
            "repair-mirror",
            "recover",
            "smart-test",
        }:
            raise ValueError("unsupported executor operation")
        arguments = [
            str(self.python_executable),
            "-m",
            "backup_system.executor",
            self.operation,
            "--run-id",
            str(self.run_id),
            "--job",
            validate_job_id(self.job_id),
        ]
        if self.operation == "check":
            if self.mode not in {"metadata", "subset", "full"}:
                raise ValueError("check requires a valid mode")
            arguments.extend(("--mode", self.mode))
        elif self.mode is not None:
            raise ValueError("mode is valid only for check")
        if self.operation in {"restore", "resolve-restore"}:
            if self.request_file is None or not self.request_file.is_absolute():
                raise ValueError("restore operation requires an absolute request file")
            arguments.extend(("--request-file", str(self.request_file)))
        elif self.request_file is not None:
            raise ValueError("request file is valid only for restore operations")
        return tuple(arguments)


class ExecutorEventDecoder:
    def __init__(self, *, max_line_bytes: int = MAX_EVENT_LINE_BYTES) -> None:
        if max_line_bytes <= 0:
            raise ValueError("maximum event line size must be positive")
        self._maximum = max_line_bytes
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[KnownExecutorEvent | UnknownExecutorEvent, ...]:
        self._buffer.extend(chunk)
        events: list[KnownExecutorEvent | UnknownExecutorEvent] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > self._maximum:
                    raise ExecutorProtocolError("executor event line exceeds size limit")
                return tuple(events)
            if newline > self._maximum:
                raise ExecutorProtocolError("executor event line exceeds size limit")
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            events.append(_decode_event(line))

    def finish(self) -> None:
        if self._buffer:
            raise ExecutorProtocolError("executor stdout ended with an incomplete event line")


def _decode_event(line: bytes) -> KnownExecutorEvent | UnknownExecutorEvent:
    if not line:
        raise ExecutorProtocolError("executor emitted an empty stdout line")
    try:
        value: Any = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutorProtocolError("executor emitted invalid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ExecutorProtocolError("executor event must be a JSON object")
    try:
        return parse_executor_event(value)
    except (TypeError, ValueError) as error:
        raise ExecutorProtocolError(
            f"executor event {value.get('event', 'unknown')!r} does not match its schema: {error}"
        ) from error
