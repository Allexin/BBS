import asyncio
from datetime import UTC, datetime
from pathlib import Path

from backup_system.common.commands import CancelCurrentCommand, RunCommand, publish_command
from backup_system.common.config import ManagerConfig
from backup_system.common.events import DiskOfflineConfirmed, RunFinished, RunStarted
from backup_system.common.ids import new_command_id
from backup_system.manager.application import ManagerApplication
from backup_system.manager.database import open_manager_database
from backup_system.manager.executor_process import ExecutorProcessResult
from backup_system.manager.layout import RuntimeLayout, initialize_data_layout
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import OperationsRepository


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
                "token_secret": "unused-token",
                "chat_id_secret": "unused-chat",
                "daily_report_cron": "0 9 * * *",
                "daily_report_timezone": "UTC",
                "stale_manager_minutes": 10,
            },
        }
    )


class _SuccessfulExecutor:
    def __init__(self, on_event: object, invocations: list[object]) -> None:
        self._on_event = on_event
        self._invocations = invocations

    async def run(self, invocation: object) -> ExecutorProcessResult:
        self._invocations.append(invocation)
        now = datetime.now(UTC)
        callback = self._on_event
        callback(
            RunStarted(
                event="run_started", timestamp=now, run_id=invocation.run_id, job_id="data"
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


class _CancellableExecutor:
    def __init__(self, on_event: object) -> None:
        self._on_event = on_event
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, invocation: object) -> ExecutorProcessResult:
        now = datetime.now(UTC)
        callback = self._on_event
        callback(
            RunStarted(
                event="run_started", timestamp=now, run_id=invocation.run_id, job_id="data"
            )
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


def test_manual_spool_command_reaches_executor_and_terminal_state(tmp_path: Path) -> None:
    root = tmp_path / "Stable"
    root.mkdir()
    (root / "backup-system.root").write_text("test", encoding="ascii")
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    operations = OperationsRepository(connection)
    config = _config()
    for job in config.jobs:
        operations.upsert_job(
            job_id=job.id,
            display_name=job.display_name,
            enabled=job.enabled,
            config_valid=True,
        )
    invocations: list[object] = []
    application = ManagerApplication(
        layout=layout,
        config=config,
        operations=operations,
        executor_factory=lambda on_event, on_stderr: _SuccessfulExecutor(
            on_event, invocations
        ),
    )
    application.initialize()
    command = RunCommand(
        command_id=new_command_id(),
        created_at=datetime.now(UTC),
        kind="run",
        job_id="data",
        operation="backup",
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
    operations.upsert_job(
        job_id="data", display_name="Data", enabled=True, config_valid=True
    )
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
            operation="backup",
        ),
    )

    async def scenario() -> None:
        assert await application.run_iteration()
        assert executor is not None
        await executor.started.wait()
        assert application.executor_active
        await application.publish("running")
        assert '"manager_state":"running"' in (
            layout.public / "health.json"
        ).read_text(encoding="utf-8")
        cancel = CancelCurrentCommand(
            command_id=new_command_id(),
            created_at=datetime.now(UTC),
            kind="cancel-current",
        )
        publish_command(root, cancel)
        assert not await application.run_iteration()
        await asyncio.wait_for(executor.cancelled.wait(), timeout=1)
        await application.wait_executor()
        assert not application.executor_active
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
    operations.upsert_job(
        job_id="data", display_name="Data", enabled=True, config_valid=True
    )

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
            str(row[0])
            for row in connection.execute("SELECT kind FROM notifications").fetchall()
        }
        assert kinds == {"run_failed", "disk_offline_unconfirmed"}
    finally:
        connection.close()
