import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backup_system.common.commands import CancelCurrentCommand, RunCommand, publish_command
from backup_system.common.config import ManagerConfig
from backup_system.common.events import (
    DiskOfflineConfirmed,
    RestoreVersionResolved,
    RunFinished,
    RunStarted,
    SnapshotCreated,
    SourceReadWarning,
)
from backup_system.common.ids import new_command_id
from backup_system.manager.application import ManagerApplication, _append_rotating_log
from backup_system.manager.database import open_manager_database
from backup_system.manager.executor_process import ExecutorProcessResult
from backup_system.manager.journal import JournalWriter
from backup_system.manager.layout import RuntimeLayout, initialize_data_layout
from backup_system.manager.log_projection import LogProjectionPublisher, PublicLogIndex
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import OperationsRepository
from backup_system.manager.service import ServiceLifecycle
from backup_system.manager.telegram import DispatchResult


class _FailingPublisher:
    def publish(self, status: object, health: object) -> None:
        del status, health
        raise PermissionError("projection target is locked")


class _RecordingNotificationDispatcher:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    async def dispatch_one(self, *, now: datetime) -> DispatchResult:
        self.calls.append(now)
        return DispatchResult.IDLE


def _config() -> ManagerConfig:
    return ManagerConfig.model_validate(
        {
            "schema_version": 1,
            "timezone": "UTC",
            "scheduler": {"poll_seconds": 5},
            "monitoring": {"volumes": {"poll_seconds": 60, "items": []}},
            "jobs": [
                {
                    "id": "data",
                    "enabled": True,
                    "display_name": "Data",
                    "schedule": {
                        "cron": "0 0 1 1 *",
                        "timezone": "UTC",
                        "cycle": [{"operation": "backup"}],
                    },
                }
            ],
            "telegram": {
                "enabled": False,
                "credentials_file": "telegram.json",
                "daily_report_cron": "0 9 * * *",
                "daily_report_timezone": "UTC",
                "stale_manager_minutes": 10,
            },
        }
    )


def test_projection_failure_does_not_stop_manager(tmp_path: Path, capsys: object) -> None:
    root = tmp_path / "Stable"
    root.mkdir()
    (root / "backup-system.root").write_text("test", encoding="ascii")
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    operations = OperationsRepository(connection)
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    application = ManagerApplication(layout=layout, config=_config(), operations=operations)
    application._projection_publisher = _FailingPublisher()  # type: ignore[assignment]
    try:
        asyncio.run(application.publish("running"))
        asyncio.run(application.publish("running"))
        assert application.accepting
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert captured.err.count("manager continues") == 1
    finally:
        connection.close()


def test_empty_notification_outbox_does_not_start_async_dispatcher(tmp_path: Path) -> None:
    root = tmp_path / "Stable"
    root.mkdir()
    (root / "backup-system.root").write_text("test", encoding="ascii")
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    operations = OperationsRepository(connection)
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    dispatcher = _RecordingNotificationDispatcher()
    application = ManagerApplication(
        layout=layout,
        config=_config(),
        operations=operations,
        notification_dispatcher=dispatcher,  # type: ignore[arg-type]
    )
    application.initialize()
    try:
        assert asyncio.run(application.run_iteration()) is False
        assert dispatcher.calls == []
    finally:
        connection.close()


def test_executor_diagnostic_log_keeps_one_bounded_rotation(tmp_path: Path) -> None:
    path = tmp_path / "executor-stderr.log"
    _append_rotating_log(path, b"123456", max_bytes=10)
    _append_rotating_log(path, b"abcdef", max_bytes=10)
    assert path.read_bytes() == b"abcdef"
    assert (tmp_path / "executor-stderr.log.1").read_bytes() == b"123456"
    _append_rotating_log(path, b"uvwxyz", max_bytes=10)
    assert path.read_bytes() == b"uvwxyz"
    assert (tmp_path / "executor-stderr.log.1").read_bytes() == b"abcdef"


class _SuccessfulExecutor:
    def __init__(self, on_event: object, invocations: list[object]) -> None:
        self._on_event = on_event
        self._invocations = invocations

    async def run(self, invocation: object) -> ExecutorProcessResult:
        self._invocations.append(invocation)
        now = datetime.now(UTC)
        callback = self._on_event
        callback(
            RunStarted(event="run_started", timestamp=now, run_id=invocation.run_id, job_id="data")
        )
        callback(DiskOfflineConfirmed(event="disk_offline_confirmed", timestamp=now))
        terminal = RunFinished(
            event="run_finished",
            timestamp=now,
            result="success",
            exit_code=0,
            disk_offline_confirmed=True,
        )
        callback(terminal)
        return ExecutorProcessResult(0, terminal)

    async def cancel_current(self) -> bool:
        return True


class _SourceWarningExecutor:
    def __init__(self, on_event: object) -> None:
        self._on_event = on_event

    async def run(self, invocation: object) -> ExecutorProcessResult:
        now = datetime.now(UTC)
        callback = self._on_event
        callback(
            RunStarted(event="run_started", timestamp=now, run_id=invocation.run_id, job_id="data")
        )
        callback(
            SnapshotCreated(
                event="snapshot_created", timestamp=now, snapshot_id="partial", bytes_added=7
            )
        )
        callback(
            SourceReadWarning(
                event="source_read_warning",
                timestamp=now,
                error_count=4,
                paths=tuple(f"T:\\bad-{index}" for index in range(4)),
            )
        )
        callback(DiskOfflineConfirmed(event="disk_offline_confirmed", timestamp=now))
        terminal = RunFinished(
            event="run_finished",
            timestamp=now,
            result="warning",
            exit_code=10,
            disk_offline_confirmed=True,
        )
        callback(terminal)
        return ExecutorProcessResult(10, terminal)

    async def cancel_current(self) -> bool:
        return True


class _CancellableExecutor:
    def __init__(self, on_event: object) -> None:
        self._on_event = on_event
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, invocation: object) -> ExecutorProcessResult:
        now = datetime.now(UTC)
        callback = self._on_event
        callback(
            RunStarted(event="run_started", timestamp=now, run_id=invocation.run_id, job_id="data")
        )
        self.started.set()
        await self.cancelled.wait()
        callback(DiskOfflineConfirmed(event="disk_offline_confirmed", timestamp=now))
        terminal = RunFinished(
            event="run_finished",
            timestamp=datetime.now(UTC),
            result="cancelled",
            exit_code=29,
            disk_offline_confirmed=True,
        )
        callback(terminal)
        return ExecutorProcessResult(29, terminal)

    async def cancel_current(self) -> bool:
        self.cancelled.set()
        return True


class _ResolvingExecutor:
    def __init__(self, on_event: object, observed: list[tuple[str, str | None]]) -> None:
        self._on_event = on_event
        self._observed = observed

    async def run(self, invocation: object) -> ExecutorProcessResult:
        version = None
        if invocation.request_file is not None:
            version = json.loads(invocation.request_file.read_text(encoding="utf-8"))["version"]
        self._observed.append((invocation.operation, version))
        now = datetime.now(UTC)
        callback = self._on_event
        callback(
            RunStarted(event="run_started", timestamp=now, run_id=invocation.run_id, job_id="data")
        )
        if invocation.operation == "resolve-restore":
            callback(
                RestoreVersionResolved(
                    event="restore_version_resolved",
                    timestamp=now,
                    version="a" * 64,
                )
            )
        callback(DiskOfflineConfirmed(event="disk_offline_confirmed", timestamp=now))
        terminal = RunFinished(
            event="run_finished",
            timestamp=now,
            result="success",
            exit_code=0,
            disk_offline_confirmed=True,
        )
        callback(terminal)
        return ExecutorProcessResult(0, terminal)

    async def cancel_current(self) -> bool:
        return True


def test_restore_latest_is_pinned_before_a_later_backup_runs(tmp_path: Path) -> None:
    root = tmp_path / "Dev"
    root.mkdir()
    (root / "backup-system.root").write_text("test", encoding="ascii")
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    operations = OperationsRepository(connection)
    config = _config()
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    observed: list[tuple[str, str | None]] = []
    application = ManagerApplication(
        layout=layout,
        config=config,
        operations=operations,
        executor_factory=lambda on_event, on_stderr: _ResolvingExecutor(on_event, observed),
    )
    application.initialize()
    accepted_at = datetime.now(UTC)
    restore = RunCommand(
        command_id=new_command_id(),
        created_at=accepted_at,
        kind="run",
        job_id="data",
        operation="restore",
        version="latest",
        path=".",
        target=r"D:\Restores",
    )
    backup = RunCommand(
        command_id=new_command_id(),
        created_at=accepted_at + timedelta(milliseconds=1),
        kind="run",
        job_id="data",
    )
    publish_command(root, restore)
    publish_command(root, backup)

    async def scenario() -> None:
        for _ in range(3):
            await application.run_iteration()
            await application.wait_executor()

    try:
        asyncio.run(scenario())
        assert observed == [
            ("resolve-restore", "latest"),
            ("restore", "a" * 64),
            ("run", None),
        ]
        request = connection.execute(
            "SELECT request_json FROM operations WHERE kind = 'restore'"
        ).fetchone()[0]
        assert json.loads(request)["version"] == "a" * 64
    finally:
        connection.close()


def test_manual_spool_command_reaches_executor_and_terminal_state(tmp_path: Path) -> None:
    root = tmp_path / "Stable"
    root.mkdir()
    (root / "backup-system.root").write_text("test", encoding="ascii")
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    operations = OperationsRepository(connection, managed_disk_jobs=set())
    config = _config()
    for job in config.jobs:
        operations.upsert_job(
            job_id=job.id,
            display_name=job.display_name,
            enabled=job.enabled,
            config_valid=True,
        )
    invocations: list[object] = []
    journal = JournalWriter(layout.logs, config.timezone)
    application = ManagerApplication(
        layout=layout,
        config=config,
        operations=operations,
        executor_factory=lambda on_event, on_stderr: _SuccessfulExecutor(on_event, invocations),
        journal=journal,
        log_projection=LogProjectionPublisher(layout.public_logs),
    )
    application.initialize()
    command = RunCommand(
        command_id=new_command_id(),
        created_at=datetime.now(UTC),
        kind="run",
        job_id="data",
    )
    publish_command(root, command)
    try:

        async def execute() -> bool:
            started = await application.run_iteration()
            await application.wait_executor()
            return started

        assert asyncio.run(execute()) is True
        assert len(invocations) == 1
        assert invocations[0].operation == "run"
        assert connection.execute(
            "SELECT result, exit_code, disk_offline_confirmed FROM runs"
        ).fetchone() == ("success", 0, 1)
        asyncio.run(application.publish("idle"))
        status = (layout.public / "status.json").read_text(encoding="utf-8")
        health = (layout.public / "health.json").read_text(encoding="utf-8")
        assert '"result":"success"' in status
        assert '"manager_state":"idle"' in health
        assert not tuple(layout.commands_incoming.iterdir())
        assert (layout.commands_completed / f"{command.command_id}.json").is_file()
        journal_days = tuple(layout.logs.glob("????-??-??.jsonl"))
        assert len(journal_days) == 1
        journal_text = journal_days[0].read_text(encoding="utf-8")
        assert '"event":"operation_started"' in journal_text
        assert '"event":"run_started"' in journal_text
        assert '"event":"run_finished"' in journal_text
        index = PublicLogIndex.model_validate_json(
            (layout.public_logs / "index.json").read_text(encoding="utf-8")
        )
        assert index.days[0].record_count == 3
        public_day = (layout.public_logs / index.days[0].file).read_text(encoding="utf-8")
        assert '"job_display_name":"Data"' in public_day
        assert '"event":"disk_offline_confirmed"' not in public_day
    finally:
        journal.close()
        connection.close()


def test_source_warning_is_persisted_with_notification(tmp_path: Path) -> None:
    root = tmp_path / "Stable"
    root.mkdir()
    (root / "backup-system.root").write_text("test", encoding="ascii")
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    notifications = NotificationRepository(connection)
    operations = OperationsRepository(connection, notifications, managed_disk_jobs=set())
    config = _config()
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    application = ManagerApplication(
        layout=layout,
        config=config,
        operations=operations,
        executor_factory=lambda on_event, on_stderr: _SourceWarningExecutor(on_event),
    )
    application.initialize()
    command = RunCommand(
        command_id=new_command_id(),
        created_at=datetime.now(UTC),
        kind="run",
        job_id="data",
    )
    publish_command(root, command)
    try:
        async def execute() -> None:
            assert await application.run_iteration()
            await application.wait_executor()

        asyncio.run(execute())
        row = connection.execute(
            "SELECT run_id, result, exit_code, snapshot_id, warning_count FROM runs"
        ).fetchone()
        assert row[1:] == ("warning", 10, "partial", 4)
        assert connection.execute(
            "SELECT kind, json_extract(payload_json, '$.error_count') FROM notifications"
        ).fetchone() == ("source_read_warning", 4)
    finally:
        connection.close()


def test_cancel_command_is_processed_while_executor_is_running(tmp_path: Path) -> None:
    root = tmp_path / "Stable"
    root.mkdir()
    (root / "backup-system.root").write_text("test", encoding="ascii")
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    operations = OperationsRepository(connection)
    config = _config()
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    executor: _CancellableExecutor | None = None

    def factory(on_event: object, on_stderr: object) -> _CancellableExecutor:
        nonlocal executor
        del on_stderr
        executor = _CancellableExecutor(on_event)
        return executor

    application = ManagerApplication(
        layout=layout,
        config=config,
        operations=operations,
        executor_factory=factory,
    )
    application.initialize()
    publish_command(
        root,
        RunCommand(
            command_id=new_command_id(),
            created_at=datetime.now(UTC),
            kind="run",
            job_id="data",
        ),
    )

    async def scenario() -> None:
        assert await application.run_iteration()
        assert executor is not None
        await executor.started.wait()
        assert application.executor_active
        await application.publish("running")
        assert '"manager_state":"running"' in (layout.public / "health.json").read_text(
            encoding="utf-8"
        )
        cancel = CancelCurrentCommand(
            command_id=new_command_id(),
            created_at=datetime.now(UTC),
            kind="cancel-current",
        )
        publish_command(root, cancel)
        assert not await application.run_iteration()
        await asyncio.wait_for(executor.cancelled.wait(), timeout=1)
        await application.wait_executor()
        await asyncio.sleep(0)
        assert not application.executor_active
        assert application.pending_cancellation_count == 0
        assert (layout.commands_completed / f"{cancel.command_id}.json").is_file()

    try:
        asyncio.run(scenario())
        assert connection.execute("SELECT result FROM runs").fetchone() == ("cancelled",)
    finally:
        connection.close()


def test_executor_transport_failure_becomes_durable_run_and_alert(tmp_path: Path) -> None:
    root = tmp_path / "Stable"
    root.mkdir()
    (root / "backup-system.root").write_text("test", encoding="ascii")
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    notifications = NotificationRepository(connection)
    operations = OperationsRepository(connection, notifications)
    config = _config()
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)

    class FailingExecutor:
        async def run(self, invocation: object) -> ExecutorProcessResult:
            del invocation
            raise RuntimeError("test transport failure")

        async def cancel_current(self) -> bool:
            return False

    application = ManagerApplication(
        layout=layout,
        config=config,
        operations=operations,
        executor_factory=lambda on_event, on_stderr: FailingExecutor(),
    )
    application.initialize()
    operations.enqueue(
        deduplication_key="test:failure",
        job_id="data",
        kind="backup",
        trigger_source="manual",
    )

    async def scenario() -> None:
        assert await application.run_iteration()
        await application.wait_executor()

    try:
        asyncio.run(scenario())
        assert connection.execute(
            "SELECT result, exit_code, disk_offline_confirmed FROM runs"
        ).fetchone() == ("failed", 30, 0)
        kinds = {
            str(row[0]) for row in connection.execute("SELECT kind FROM notifications").fetchall()
        }
        assert kinds == {"run_failed", "disk_offline_unconfirmed"}
    finally:
        connection.close()


def test_service_shutdown_cancels_live_executor_and_discards_tail(tmp_path: Path) -> None:
    root = tmp_path / "Stable"
    root.mkdir()
    (root / "backup-system.root").write_text("test", encoding="ascii")
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    operations = OperationsRepository(connection)
    config = _config()
    operations.upsert_job(job_id="data", display_name="Data", enabled=True, config_valid=True)
    executor: _CancellableExecutor | None = None

    def factory(on_event: object, on_stderr: object) -> _CancellableExecutor:
        nonlocal executor
        del on_stderr
        executor = _CancellableExecutor(on_event)
        return executor

    application = ManagerApplication(
        layout=layout,
        config=config,
        operations=operations,
        executor_factory=factory,
    )
    application.initialize()
    operations.enqueue(
        deduplication_key="test:active",
        job_id="data",
        kind="backup",
        trigger_source="manual",
    )

    async def scenario() -> None:
        assert await application.run_iteration()
        assert executor is not None
        await executor.started.wait()
        queued = operations.enqueue(
            deduplication_key="test:tail",
            job_id="data",
            kind="check",
            mode="full",
            trigger_source="manual",
        )
        lifecycle = ServiceLifecycle(
            operations=operations,
            stop_accepting=application.stop_accepting,
            cancel_executor=application.cancel_executor,
            wait_executor=application.wait_executor,
            publish_final_status=lambda: application.publish("stopping"),
        )
        await lifecycle.shutdown()
        state = connection.execute(
            "SELECT state FROM operations WHERE operation_id = ?",
            (str(queued.operation_id),),
        ).fetchone()
        assert state == ("discarded_on_service_stop",)

    try:
        asyncio.run(scenario())
        assert connection.execute("SELECT result FROM runs ORDER BY started_at").fetchone() == (
            "cancelled",
        )
        assert '"manager_state":"stopping"' in (layout.public / "health.json").read_text(
            encoding="utf-8"
        )
    finally:
        connection.close()
