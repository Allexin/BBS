"""Asynchronous executor subprocess transport with cooperative cancellation."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

from backup_system.common.events import KnownExecutorEvent, RunFinished, UnknownExecutorEvent
from backup_system.manager.executor_protocol import (
    ExecutorEventDecoder,
    ExecutorInvocation,
    ExecutorProtocolError,
)
from backup_system.manager.win32_job import KillOnCloseJob

EventHandler = Callable[[KnownExecutorEvent | UnknownExecutorEvent], None]
DiagnosticHandler = Callable[[bytes], None]


class ExecutorProcessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutorProcessResult:
    exit_code: int
    terminal_event: RunFinished


class ExecutorProcessRunner:
    def __init__(
        self,
        *,
        on_event: EventHandler,
        on_stderr: DiagnosticHandler,
        use_job_object: bool | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_stderr = on_stderr
        self._process: asyncio.subprocess.Process | None = None
        self._run_lock = asyncio.Lock()
        self._cancel_sent = False
        self._use_job_object = os.name == "nt" if use_job_object is None else use_job_object

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None

    async def run(self, invocation: ExecutorInvocation) -> ExecutorProcessResult:
        if self._run_lock.locked():
            raise ExecutorProcessError("an executor process is already running")
        async with self._run_lock:
            executor_argv = invocation.argv()
            use_job = self._use_job_object
            argv = (
                (
                    sys.executable,
                    "-m",
                    "backup_system.manager.executor_supervisor",
                    "--",
                    *executor_argv,
                )
                if use_job
                else executor_argv
            )
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._process = process
            self._cancel_sent = False
            job = KillOnCloseJob() if use_job else None
            try:
                if job is not None:
                    job.assign(process.pid)
                    assert process.stdin is not None
                    process.stdin.write(b"start\n")
                    await process.stdin.drain()
                stdout_task = asyncio.create_task(self._consume_stdout(process))
                stderr_task = asyncio.create_task(self._consume_stderr(process))
                exit_code = await process.wait()
                terminal, protocol_error = await stdout_task
                await stderr_task
            finally:
                self._process = None
                if job is not None:
                    job.close()

            if protocol_error is not None:
                raise protocol_error
            if terminal is None:
                raise ExecutorProcessError("executor exited without a terminal event")
            if terminal.exit_code != exit_code:
                raise ExecutorProcessError(
                    "executor terminal event conflicts with process exit code"
                )
            return ExecutorProcessResult(exit_code=exit_code, terminal_event=terminal)

    async def cancel_current(self) -> bool:
        process = self._process
        if process is None or process.returncode is not None or self._cancel_sent:
            return False
        stream = process.stdin
        if stream is None or stream.is_closing():
            return False
        self._cancel_sent = True
        stream.write(b"cancel\n")
        try:
            await stream.drain()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    async def _consume_stdout(
        self, process: asyncio.subprocess.Process
    ) -> tuple[RunFinished | None, ExecutorProtocolError | None]:
        stream = process.stdout
        if stream is None:
            raise ExecutorProcessError("executor stdout pipe was not created")
        decoder = ExecutorEventDecoder()
        terminal: RunFinished | None = None
        protocol_error: ExecutorProtocolError | None = None
        while chunk := await stream.read(64 * 1024):
            if protocol_error is not None:
                continue
            try:
                events = decoder.feed(chunk)
                for event in events:
                    if isinstance(event, RunFinished):
                        if terminal is not None:
                            raise ExecutorProtocolError(
                                "executor emitted more than one terminal event"
                            )
                        terminal = event
                    self._on_event(event)
                    if isinstance(event, RunFinished):
                        stdin = process.stdin
                        if stdin is not None and not stdin.is_closing():
                            stdin.close()
            except (ExecutorProtocolError, ValueError, TypeError) as error:
                protocol_error = (
                    error
                    if isinstance(error, ExecutorProtocolError)
                    else ExecutorProtocolError(
                        "executor event handler rejected "
                        f"{type(event).__name__}: {type(error).__name__}: {error}"
                    )
                )
                await self.cancel_current()
        if protocol_error is None:
            try:
                decoder.finish()
            except ExecutorProtocolError as error:
                protocol_error = error
        return terminal, protocol_error

    async def _consume_stderr(self, process: asyncio.subprocess.Process) -> None:
        stream = process.stderr
        if stream is None:
            raise ExecutorProcessError("executor stderr pipe was not created")
        while chunk := await stream.read(64 * 1024):
            self._on_stderr(chunk)
