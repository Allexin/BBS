import json
from pathlib import Path
from uuid import UUID

import pytest

from backup_system.common.events import RunStarted, UnknownExecutorEvent
from backup_system.manager.executor_protocol import (
    ExecutorEventDecoder,
    ExecutorInvocation,
    ExecutorProtocolError,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


def _line(event: str = "run_started") -> bytes:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "event": event,
                "timestamp": "2026-01-02T03:04:05+00:00",
                "run_id": str(RUN_ID),
                "job_id": "job-1",
            }
        ).encode("utf-8")
        + b"\n"
    )


def test_decoder_handles_split_and_multiple_lines() -> None:
    decoder = ExecutorEventDecoder()
    payload = _line() + _line("future_event")
    assert decoder.feed(payload[:17]) == ()
    events = decoder.feed(payload[17:])
    decoder.finish()
    assert isinstance(events[0], RunStarted)
    assert isinstance(events[1], UnknownExecutorEvent)


@pytest.mark.parametrize("payload", [b"not-json\n", b"\xff\n", b"[]\n", b"\n"])
def test_invalid_stdout_is_rejected_without_echoing_payload(payload: bytes) -> None:
    with pytest.raises(ExecutorProtocolError) as raised:
        ExecutorEventDecoder().feed(payload)
    assert "not-json" not in str(raised.value)


def test_oversized_and_incomplete_lines_are_rejected() -> None:
    decoder = ExecutorEventDecoder(max_line_bytes=10)
    with pytest.raises(ExecutorProtocolError, match="size limit"):
        decoder.feed(b"x" * 11)
    decoder = ExecutorEventDecoder()
    decoder.feed(b"{")
    with pytest.raises(ExecutorProtocolError, match="incomplete"):
        decoder.finish()


def test_argv_is_positional_and_operation_specific(tmp_path: Path) -> None:
    executable = tmp_path / "backup-executor.exe"
    invocation = ExecutorInvocation(
        executable=executable,
        operation="check",
        run_id=RUN_ID,
        job_id="job-1",
        mode="full",
    )
    assert invocation.argv() == (
        str(executable),
        "check",
        "--run-id",
        str(RUN_ID),
        "--job",
        "job-1",
        "--mode",
        "full",
    )


def test_restore_requires_absolute_request_file(tmp_path: Path) -> None:
    invocation = ExecutorInvocation(
        executable=tmp_path / "backup-executor.exe",
        operation="restore",
        run_id=RUN_ID,
        job_id="job-1",
        request_file=Path("relative.json"),
    )
    with pytest.raises(ValueError, match="absolute"):
        invocation.argv()
