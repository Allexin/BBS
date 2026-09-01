"""Production manager composition for commands, schedules, and executor runs."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from backup_system import __version__
from backup_system.common.config import ManagerConfig, SmartConfig
from backup_system.common.events import (
    DiskOfflineConfirmed,
    DiskOfflineFailed,
    KnownExecutorEvent,
    Progress,
    RunFinished,
    SmartObserved,
    SmartTestDiskFinished,
    SourceReadWarning,
    StageChanged,
    UnknownExecutorEvent,
)
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.time import utc_now
from backup_system.manager.command_processor import CommandProcessor
from backup_system.manager.daily_reports import DailyReportStore
from backup_system.manager.deadlines import DeadlineMonitor, deadline_for
from backup_system.manager.executor_events import ExecutorEventIngestor, ExecutorRunEventProcessor
from backup_system.manager.executor_process import ExecutorProcessResult, ExecutorProcessRunner
from backup_system.manager.executor_protocol import ExecutorInvocation
from backup_system.manager.journal import JournalWriter, Severity
from backup_system.manager.layout import RuntimeLayout
from backup_system.manager.log_projection import LogProjectionPublisher
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import ClaimedRun, OperationsRepository, RunResult
from backup_system.manager.projection_builder import ProjectionBuilder
from backup_system.manager.public_projection import ProjectionPublisher
from backup_system.manager.schedule_store import ScheduleStore
from backup_system.manager.scheduler_events import SchedulerEventRepository
from backup_system.manager.smart_history import SmartHistoryRepository
from backup_system.manager.spool import CommandSpool
from backup_system.manager.startup_reports import StartupReportPlanner
from backup_system.manager.telegram import AsyncNotificationDispatcher


class ExecutorTransport(Protocol):
    async def run(self, invocation: ExecutorInvocation) -> ExecutorProcessResult: ...

    async def cancel_current(self) -> bool: ...


ExecutorFactory = Callable[
    [
        Callable[[KnownExecutorEvent | UnknownExecutorEvent], None],
        Callable[[bytes], None],
    ],
    ExecutorTransport,
]

_EXECUTOR_LOG_MAX_BYTES = 10 * 1024 * 1024


def _default_executor_factory(
    on_event: Callable[[KnownExecutorEvent | UnknownExecutorEvent], None],
    on_stderr: Callable[[bytes], None],
) -> ExecutorTransport:
    return ExecutorProcessRunner(on_event=on_event, on_stderr=on_stderr)


class ManagerApplication:
    """Own the connected manager components and serialize all mutable work."""

    def __init__(
        self,
        *,
        layout: RuntimeLayout,
        config: ManagerConfig,
        operations: OperationsRepository,
        executor_factory: ExecutorFactory = _default_executor_factory,
        job_kinds: dict[str, Literal["snapshot", "mirror", "maintenance", "smart-test"]]
        | None = None,
        job_protection_info: dict[str, str] | None = None,
        notification_dispatcher: AsyncNotificationDispatcher | None = None,
        smart_config: SmartConfig | None = None,
        journal: JournalWriter | None = None,
        log_projection: LogProjectionPublisher | None = None,
    ) -> None:
        self._layout = layout
        self._config = config
        self._operations = operations
        self._executor_factory = executor_factory
        self._notification_dispatcher = notification_dispatcher
        self._journal = journal
        self._log_projection = log_projection
        self._journal_timezone = ZoneInfo(config.timezone)
        self._job_display_names = {job.id: job.display_name for job in config.jobs}
        self._accepting = True
        self._active_executor: ExecutorTransport | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._cancellation_tasks: set[asyncio.Task[bool]] = set()
        self._started_at = utc_now()
        self._last_health = "unknown"
        self._projection_failure_reported = False
        self._diagnostic_lock = threading.Lock()
        selected_job_kinds = job_kinds or {}
        connection = operations.connection
        notifications = NotificationRepository(connection)
        self._notifications = notifications
        self._spool = CommandSpool(layout)
        self._commands = CommandProcessor(
            self._spool,
            operations,
            cancel_current=self.request_executor_cancel,
            default_operations={job.id: job.schedule.cycle[0].operation for job in config.jobs},
        )
        self._schedules = ScheduleStore(
            connection,
            operations,
            SchedulerEventRepository(connection),
            notifications,
        )
        self._deadlines = DeadlineMonitor(connection, notifications)
        self._daily_reports = DailyReportStore(connection, notifications)
        self._startup_reports = StartupReportPlanner(connection, notifications)
        self._smart = ExecutorEventIngestor(SmartHistoryRepository(connection, notifications))
        self._projection_builder = ProjectionBuilder(
            connection,
            job_kinds=selected_job_kinds,
            job_protection_info=job_protection_info,
            job_deadlines={job.id: job.schedule.deadline for job in config.jobs},
            disk_health_policies={
                item.disk_id: (item.affects_system_health, item.reason)
                for item in config.monitoring.smart.health_policies
            },
            smart_stale_after_hours=(
                smart_config.stale_after_hours if smart_config is not None else 48
            ),
            status_stale_after_seconds=max(60, config.scheduler.poll_seconds * 3),
        )
        self._projection_publisher = ProjectionPublisher(layout.public)

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def executor_active(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    @property
    def pending_cancellation_count(self) -> int:
        return len(self._cancellation_tasks)

    def initialize(self, *, new_job_ids: set[str] | None = None) -> None:
        now = utc_now()
        if self._log_projection is not None:
            self._log_projection.publish_index(generated_at=now)
        for job in self._config.jobs:
            if new_job_ids is not None and job.id in new_job_ids:
                self._schedules.initialize_new_job(job.id, job.schedule, now=now)
            self._schedules.reconcile_startup(job.id, job.schedule, now=now)
        telegram = self._config.telegram
        self._daily_reports.reconcile_startup(
            cron=telegram.daily_report_cron,
            timezone=telegram.daily_report_timezone,
            now=now,
        )

    def stop_accepting(self) -> None:
        self._accepting = False

    def plan_startup_report(
        self,
        *,
        previous_seen_at: datetime,
        interrupted: tuple[str, ...],
    ) -> None:
        self._startup_reports.enqueue(
            started_at=self._started_at,
            previous_seen_at=previous_seen_at,
            interrupted=interrupted,
        )

    def journal_startup_interruptions(self, run_ids: tuple[str, ...]) -> None:
        for run_id_text in run_ids:
            row = self._operations.connection.execute(
                """SELECT runs.operation_id, runs.run_id, runs.job_id, operations.kind
                FROM runs JOIN operations ON operations.operation_id = runs.operation_id
                WHERE runs.run_id = ?""",
                (run_id_text,),
            ).fetchone()
            if row is None:
                continue
            self._write_journal(
                severity="error",
                event="run_interrupted",
                operation_id=UUID(str(row[0])),
                run_id=UUID(str(row[1])),
                job_id=str(row[2]),
                details={
                    "operation_kind": str(row[3]),
                    "public_reason": "Run interrupted by manager restart",
                },
            )

    async def publish(
        self, state: Literal["starting", "idle", "running", "stopping", "error"]
    ) -> None:
        now = utc_now()
        status, health = self._projection_builder.build(
            now=now,
            manager_started_at=self._started_at,
            manager_state=state,
            version=__version__,
        )
        self._last_health = status.overall_health
        try:
            self._projection_publisher.publish(status, health)
        except OSError as error:
            if not self._projection_failure_reported:
                print(
                    f"status projection publication failed; manager continues: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                self._projection_failure_reported = True
        else:
            self._projection_failure_reported = False

    def request_executor_cancel(self) -> None:
        executor = self._active_executor
        if executor is not None:
            task = asyncio.get_running_loop().create_task(executor.cancel_current())
            self._cancellation_tasks.add(task)
            task.add_done_callback(self._cancellation_finished)

    async def cancel_executor(self) -> None:
        executor = self._active_executor
        if executor is not None:
            await executor.cancel_current()
        pending = tuple(self._cancellation_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _cancellation_finished(self, task: asyncio.Task[bool]) -> None:
        self._cancellation_tasks.discard(task)
        with suppress(asyncio.CancelledError):
            error = task.exception()
            if error is not None:
                self._write_executor_diagnostic(
                    f"executor cancellation failed: {type(error).__name__}\n".encode("ascii")
                )

    async def wait_executor(self) -> None:
        task = self._active_task
        if task is not None:
            await task
            self._active_task = None

    async def run_iteration(self) -> bool:
        """Perform one bounded manager iteration; return whether a run was executed."""
        if self._accepting:
            self._spool.accept_incoming()
            self._commands.process_accepted()
            now = utc_now()
            for job in self._config.jobs:
                if job.enabled:
                    self._schedules.poll(
                        job.id,
                        job.schedule,
                        now=now,
                        poll_seconds=self._config.scheduler.poll_seconds,
                    )
            self._deadlines.sweep(now=now)
            telegram = self._config.telegram
            self._daily_reports.poll(
                cron=telegram.daily_report_cron,
                timezone=telegram.daily_report_timezone,
                health=self._last_health,
                now=now,
                poll_seconds=self._config.scheduler.poll_seconds,
            )
            await self._dispatch_notification(now)
        if not self._accepting:
            return False
        if self._active_task is not None:
            if not self._active_task.done():
                return False
            await self._active_task
            self._active_task = None
        claimed = self._operations.claim_next()
        if claimed is None:
            return False
        self._assign_deadline(claimed)
        self._write_journal(
            severity="info",
            event="operation_started",
            operation_id=claimed.operation_id,
            run_id=claimed.run_id,
            job_id=claimed.job_id,
            details={"operation_kind": claimed.kind},
        )
        self._active_task = asyncio.create_task(self._execute(claimed))
        await asyncio.sleep(0)
        if self._active_task.done():
            await self._active_task
            self._active_task = None
        return True

    async def _execute(self, claimed: ClaimedRun) -> None:
        processor = ExecutorRunEventProcessor(
            run_id=claimed.run_id,
            job_id=claimed.job_id,
            operations=self._operations,
            smart=self._smart,
        )

        def on_event(event: KnownExecutorEvent | UnknownExecutorEvent) -> None:
            if isinstance(event, UnknownExecutorEvent):
                self._write_journal(
                    severity="warning",
                    event="unknown_executor_event",
                    operation_id=claimed.operation_id,
                    run_id=claimed.run_id,
                    job_id=claimed.job_id,
                    details={"operation_kind": claimed.kind},
                    timestamp=event.timestamp,
                )
                return
            processor.process(event)
            if isinstance(event, SourceReadWarning):
                self._publish_source_error_report(claimed.run_id, event)
            self._journal_executor_event(claimed, event)

        executor = self._executor_factory(on_event, self._write_executor_diagnostic)
        self._active_executor = executor
        request_file: Path | None = None
        try:
            if claimed.kind in {"resolve-restore", "restore"}:
                request_file = self._write_restore_request(claimed)
            invocation = ExecutorInvocation(
                python_executable=Path(sys.executable).resolve(),
                operation=_executor_operation(claimed.kind),
                run_id=claimed.run_id,
                job_id=claimed.job_id,
                mode=claimed.mode,
                request_file=request_file,
            )
            result = await executor.run(invocation)
            self._record_schedule_completion(claimed, result)
        except Exception as error:
            self._write_executor_diagnostic(
                f"manager classified executor failure: {type(error).__name__}\n".encode("ascii")
            )
            if self._operations.run_is_active(claimed.run_id):
                self._operations.finish_run(
                    claimed.run_id,
                    result=RunResult.FAILED,
                    exit_code=int(ExecutorExitCode.INTERNAL_ERROR),
                    disk_offline_confirmed=False,
                )
            self._write_journal(
                severity="error",
                event="executor_transport_failed",
                operation_id=claimed.operation_id,
                run_id=claimed.run_id,
                job_id=claimed.job_id,
                details={
                    "operation_kind": claimed.kind,
                    "public_reason": "Executor transport failed",
                },
            )
        finally:
            self._active_executor = None
            if request_file is not None:
                request_file.unlink(missing_ok=True)

    def _publish_source_error_report(self, run_id: UUID, event: SourceReadWarning) -> None:
        directory = self._layout.public / "source-errors"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{run_id}.txt"
        temporary = directory / f".{run_id}.txt.tmp"
        lines = [
            f"Source read errors: {event.error_count}",
            f"Generated: {event.timestamp.isoformat()}",
            "",
            *event.paths,
        ]
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(lines) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_restore_request(self, claimed: ClaimedRun) -> Path:
        if claimed.request is None:
            raise ValueError("restore operation has no request")
        path = self._layout.temp / f"restore-{claimed.run_id}.json"
        with path.open("xb") as stream:
            stream.write(json.dumps(claimed.request, separators=(",", ":")).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        return path.resolve()

    def _assign_deadline(self, claimed: ClaimedRun) -> None:
        if claimed.scheduled_at is None:
            self._deadlines.assign(claimed.run_id, None)
            return
        job = next((item for item in self._config.jobs if item.id == claimed.job_id), None)
        if job is None:
            raise ValueError("claimed run has no manager job configuration")
        self._deadlines.assign(
            claimed.run_id,
            deadline_for(job.schedule, claimed.scheduled_at),
        )

    def _record_schedule_completion(
        self, claimed: ClaimedRun, result: ExecutorProcessResult
    ) -> None:
        job = next((item for item in self._config.jobs if item.id == claimed.job_id), None)
        if job is None or claimed.kind == "resolve-restore":
            return
        self._schedules.record_completion(
            claimed.job_id,
            job.schedule,
            result=result.terminal_event.result,
            trigger_source=claimed.trigger_source,
            operation=claimed.kind,
            check_mode=claimed.mode,
            completed_at=result.terminal_event.timestamp,
        )

    def _write_executor_diagnostic(self, chunk: bytes) -> None:
        path = self._layout.logs / "executor-stderr.log"
        with self._diagnostic_lock:
            _append_rotating_log(path, chunk, max_bytes=_EXECUTOR_LOG_MAX_BYTES)

    def _journal_executor_event(self, claimed: ClaimedRun, event: KnownExecutorEvent) -> None:
        if isinstance(event, Progress):
            return
        if isinstance(event, DiskOfflineConfirmed) and not self._operations.requires_disk_offline(
            claimed.job_id
        ):
            return
        severity: Severity = "info"
        details: dict[str, object] = {"operation_kind": claimed.kind}
        if isinstance(event, StageChanged):
            details["stage"] = event.stage
        elif isinstance(event, RunFinished):
            if event.result == "warning":
                severity = "warning"
            elif event.result in {"failed", "interrupted"}:
                severity = "error"
            details["public_reason"] = f"Run finished: {event.result}"
        elif isinstance(event, SourceReadWarning):
            severity = "warning"
            details["public_reason"] = f"Source read errors: {event.error_count}"
        elif isinstance(event, DiskOfflineFailed):
            severity = "error"
            details["public_reason"] = "Backup disk offline was not confirmed"
        elif isinstance(event, SmartObserved) and event.health in {"warning", "critical"}:
            severity = "warning" if event.health == "warning" else "error"
            details["public_reason"] = f"SMART health is {event.health}"
        elif isinstance(event, SmartTestDiskFinished) and event.result != "success":
            severity = "warning" if event.result == "unsupported" else "error"
            details["public_reason"] = f"SMART self-test result: {event.result}"
        self._write_journal(
            severity=severity,
            event=event.event,
            operation_id=claimed.operation_id,
            run_id=claimed.run_id,
            job_id=claimed.job_id,
            details=details,
            timestamp=event.timestamp,
        )

    def _write_journal(
        self,
        *,
        severity: Severity,
        event: str,
        operation_id: UUID | None,
        run_id: UUID | None,
        job_id: str,
        details: dict[str, object],
        timestamp: datetime | None = None,
    ) -> None:
        journal = self._journal
        if journal is None:
            return
        try:
            record = journal.write(
                severity=severity,
                component="manager",
                event=event,
                operation_id=operation_id,
                run_id=run_id,
                job_id=job_id,
                details=details,
                timestamp=timestamp,
            )
            projection = self._log_projection
            if projection is not None:
                local_date = record.timestamp.astimezone(self._journal_timezone).date()
                projection.publish_day(
                    self._layout.logs / f"{local_date.isoformat()}.jsonl",
                    local_date=local_date,
                    updated_at=record.timestamp,
                    job_display_names=self._job_display_names,
                )
        except OSError as error:
            print(
                f"runtime journal publication failed; manager continues: {error}",
                file=sys.stderr,
                flush=True,
            )

    async def _dispatch_notification(self, now: datetime) -> None:
        dispatcher = self._notification_dispatcher
        if dispatcher is None or self._notifications.next_due(now=now) is None:
            return
        try:
            await dispatcher.dispatch_one(now=now)
        except Exception as error:
            self._write_executor_diagnostic(
                f"notification dispatcher failed: {type(error).__name__}\n".encode("ascii")
            )


def _append_rotating_log(path: Path, chunk: bytes, *, max_bytes: int) -> None:
    if max_bytes <= 0:
        raise ValueError("log size limit must be positive")
    payload = chunk[-max_bytes:]
    current_size = path.stat().st_size if path.is_file() else 0
    if current_size and current_size + len(payload) > max_bytes:
        os.replace(path, path.with_name(f"{path.name}.1"))
    with path.open("ab") as stream:
        stream.write(payload)
        stream.flush()


def _executor_operation(kind: str) -> str:
    return "run" if kind == "backup" else kind
