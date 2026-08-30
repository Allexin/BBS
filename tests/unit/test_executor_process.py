import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest

from backup_system.common.events import RunFinished
from backup_system.manager.executor_process import ExecutorProcessError, ExecutorProcessRunner
from backup_system.manager.executor_protocol import ExecutorInvocation, ExecutorProtocolError

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


def _event(name: str, **values: object) -> bytes:
    payload = {
        "schema_version": 1,
        "event": name,
        "timestamp": "2026-01-02T03:04:05+00:00",
        **values,
    }
    return json.dumps(payload).encode() + b"\n"


class _Reader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, size: int = -1) -> bytes:
        del size
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""


class _Writer:
    def __init__(self) -> None:
        self.payload = bytearray()

    def is_closing(self) -> bool:
        return False

    def write(self, data: bytes) -> None:
        self.payload.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _Process:
    def __init__(self, stdout: list[bytes], stderr: list[bytes], exit_code: int) -> None:
        self.stdout = _Reader(stdout)
        self.stderr = _Reader(stderr)
        self.stdin = _Writer()
        self.returncode: int | None = None
        self._exit_code = exit_code

    async def wait(self) -> int:
        await asyncio.sleep(0.01)
        self.returncode = self._exit_code
        return self._exit_code


def _invocation(tmp_path: Path) -> ExecutorInvocation:
    return ExecutorInvocation(
        python_executable=tmp_path / "python.exe",
        operation="run",
        run_id=RUN_ID,
        job_id="job-1",
    )


def test_runner_drains_both_streams_and_validates_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout = [
        _event("run_started", run_id=str(RUN_ID), job_id="job-1"),
        _event("disk_offline_confirmed")
        + _event("run_finished", result="success", exit_code=0, disk_offline_confirmed=True),
    ]
    process = _Process(stdout, [b"diagnostic"], 0)

    async def spawn(*args: object, **kwargs: object) -> _Process:
        assert args[:3] == (str(tmp_path / "python.exe"), "-m", "backup_system.executor")
        assert set(kwargs) == {"stdin", "stdout", "stderr"}
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    events: list[object] = []
    diagnostics: list[bytes] = []
    runner = ExecutorProcessRunner(
        on_event=events.append, on_stderr=diagnostics.append, use_job_object=False
    )

    result = asyncio.run(runner.run(_invocation(tmp_path)))

    assert isinstance(result.terminal_event, RunFinished)
    assert result.exit_code == 0
    assert diagnostics == [b"diagnostic"]
    assert len(events) == 3


def test_protocol_failure_requests_cancel_and_still_drains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = _Process([b"not-json\n", b"ignored after failure"], [b"diagnostic"], 30)

    async def spawn(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    diagnostics: list[bytes] = []
    runner = ExecutorProcessRunner(
        on_event=lambda event: None, on_stderr=diagnostics.append, use_job_object=False
    )

    with pytest.raises(ExecutorProtocolError):
        asyncio.run(runner.run(_invocation(tmp_path)))

    assert process.stdin.payload == b"cancel\n"
    assert diagnostics == [b"diagnostic"]


def test_exit_code_must_match_terminal_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    terminal = _event("run_finished", result="failed", exit_code=30, disk_offline_confirmed=False)
    process = _Process([terminal], [], 29)

    async def spawn(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runner = ExecutorProcessRunner(
        on_event=lambda event: None, on_stderr=lambda chunk: None, use_job_object=False
    )

    with pytest.raises(ExecutorProcessError, match="conflicts"):
        asyncio.run(runner.run(_invocation(tmp_path)))


def test_external_cancel_is_sent_only_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    terminal = _event(
        "run_finished", result="cancelled", exit_code=29, disk_offline_confirmed=False
    )
    process = _Process([terminal], [], 29)

    async def spawn(*args: object, **kwargs: object) -> _Process:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runner = ExecutorProcessRunner(
        on_event=lambda event: None, on_stderr=lambda chunk: None, use_job_object=False
    )

    async def scenario() -> None:
        task = asyncio.create_task(runner.run(_invocation(tmp_path)))
        await asyncio.sleep(0)
        assert await runner.cancel_current() is True
        assert await runner.cancel_current() is False
        result = await task
        assert result.exit_code == 29

    asyncio.run(scenario())
    assert process.stdin.payload == b"cancel\n"
