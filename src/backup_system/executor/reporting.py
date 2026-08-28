"""JSON Lines run envelope with exactly one normalized terminal event."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TextIO
from uuid import UUID

from backup_system.common.events import (
    DiskOfflineConfirmed,
    DiskOfflineFailed,
    EventBase,
    RunFinished,
    RunStarted,
)
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.time import utc_now
from backup_system.executor.cancellation import CancellationRequested
from backup_system.executor.disk_control import (
    DiskIdentityMismatchError,
    DiskNotFoundError,
)
from backup_system.executor.lifecycle import LifecycleCleanupError, LifecycleOperationError


@dataclass(frozen=True, slots=True)
class ExecutorOutcome:
    result: str
    exit_code: ExecutorExitCode
    disk_offline_confirmed: bool


class EventSink(Protocol):
    def emit(self, event: EventBase) -> None: ...


class JsonLineEventSink:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: EventBase) -> None:
        self._stream.write(event.model_dump_json() + "\n")
        self._stream.flush()


class ExecutorRunReporter:
    def __init__(
        self,
        sink: EventSink,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sink = sink
        self._clock = clock

    def execute(
        self,
        *,
        run_id: UUID,
        job_id: str,
        operation: Callable[[], object],
    ) -> ExecutorOutcome:
        self._sink.emit(
            RunStarted(event="run_started", timestamp=self._clock(), run_id=run_id, job_id=job_id)
        )
        try:
            operation()
        except BaseException as error:
            outcome = _failure_outcome(error)
        else:
            outcome = ExecutorOutcome("success", ExecutorExitCode.SUCCESS, True)

        disk_event: EventBase
        if outcome.disk_offline_confirmed:
            disk_event = DiskOfflineConfirmed(
                event="disk_offline_confirmed", timestamp=self._clock()
            )
        else:
            disk_event = DiskOfflineFailed(event="disk_offline_failed", timestamp=self._clock())
        self._sink.emit(disk_event)
        self._sink.emit(
            RunFinished(
                event="run_finished",
                timestamp=self._clock(),
                result=outcome.result,  # type: ignore[arg-type]
                exit_code=int(outcome.exit_code),
                disk_offline_confirmed=outcome.disk_offline_confirmed,
            )
        )
        return outcome


def _failure_outcome(error: BaseException) -> ExecutorOutcome:
    if isinstance(error, LifecycleCleanupError):
        return ExecutorOutcome("failed", ExecutorExitCode.DISK_OFFLINE_FAILED, False)
    if isinstance(error, LifecycleOperationError):
        primary = _failure_outcome(error.primary_error)
        return ExecutorOutcome(primary.result, primary.exit_code, True)
    if isinstance(error, DiskNotFoundError):
        return ExecutorOutcome("failed", ExecutorExitCode.DISK_NOT_FOUND, False)
    if isinstance(error, DiskIdentityMismatchError):
        return ExecutorOutcome("failed", ExecutorExitCode.DISK_IDENTITY_MISMATCH, False)
    if isinstance(error, (CancellationRequested, KeyboardInterrupt)):
        return ExecutorOutcome("cancelled", ExecutorExitCode.CANCELLED, False)
    return ExecutorOutcome("failed", ExecutorExitCode.INTERNAL_ERROR, False)
