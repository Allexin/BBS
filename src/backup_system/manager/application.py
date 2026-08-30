"""Production manager composition for commands, schedules, and executor runs."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from backup_system import __version__
from backup_system.common.config import ManagerConfig
from backup_system.common.events import KnownExecutorEvent, UnknownExecutorEvent
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.time import utc_now
from backup_system.manager.command_processor import CommandProcessor
from backup_system.manager.daily_reports import DailyReportStore
from backup_system.manager.deadlines import DeadlineMonitor, deadline_for
from backup_system.manager.executor_events import ExecutorEventIngestor, ExecutorRunEventProcessor
from backup_system.manager.executor_process import ExecutorProcessResult, ExecutorProcessRunner
from backup_system.manager.executor_protocol import ExecutorInvocation
from backup_system.manager.layout import RuntimeLayout
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
        job_kinds: dict[str, Literal["snapshot", "mirror", "maintenance"]] | None = None,
        notification_dispatcher: AsyncNotificationDispatcher | None = None,
    ) -> None:
        self._layout = layout
        self._config = config
        self._operations = operations
        self._executor_factory = executor_factory
        self._notification_dispatcher = notification_dispatcher
        self._accepting = True
        self._active_executor: ExecutorTransport | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._started_at = utc_now()
        self._last_health = "unknown"
        connection = operations.connection
        notifications = NotificationRepository(connection)
        self._spool = CommandSpool(layout)
        self._commands = CommandProcessor(
            self._spool,
            operations,
            cancel_current=self.request_executor_cancel,
            resolve_restore_version=self._resolve_restore_version,
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
        self._smart = ExecutorEventIngestor(
            SmartHistoryRepository(connection, notifications)
        )
        self._projection_builder = ProjectionBuilder(
            connection,
            job_kinds=job_kinds,
            job_deadlines={job.id: job.schedule.deadline for job in config.jobs},
        )
        self._projection_publisher = ProjectionPublisher(layout.public)

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def executor_active(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    def initialize(self) -> None:
        now = utc_now()
        for job in self._config.jobs:
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
        self._projection_publisher.publish(status, health)

    def request_executor_cancel(self) -> None:
        executor = self._active_executor
        if executor is not None:
            asyncio.get_running_loop().create_task(executor.cancel_current())

    async def cancel_executor(self) -> None:
        executor = self._active_executor
        if executor is not None:
            await executor.cancel_current()

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
                return
            processor.process(event)

        executor = self._executor_factory(on_event, self._write_executor_diagnostic)
        self._active_executor = executor
        request_file: Path | None = None
        try:
            if claimed.kind == "restore":
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
        finally:
            self._active_executor = None
            if request_file is not None:
                request_file.unlink(missing_ok=True)

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
        if job is None:
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
        with path.open("ab") as stream:
            stream.write(chunk)
            stream.flush()

    async def _dispatch_notification(self, now: datetime) -> None:
        dispatcher = self._notification_dispatcher
        if dispatcher is None:
            return
        try:
            await dispatcher.dispatch_one(now=now)
        except Exception as error:
            self._write_executor_diagnostic(
                f"notification dispatcher failed: {type(error).__name__}\n".encode("ascii")
            )

    @staticmethod
    def _resolve_restore_version(job_id: str, version: str) -> str:
        del job_id
        if version == "latest":
            raise ValueError("latest restore version requires repository resolution")
        return version


def _executor_operation(kind: str) -> str:
    return "run" if kind == "backup" else kind
