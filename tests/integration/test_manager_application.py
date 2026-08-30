import asyncio
from datetime import UTC, datetime
from pathlib import Path

from backup_system.common.commands import RunCommand, publish_command
from backup_system.common.config import ManagerConfig
from backup_system.common.events import DiskOfflineConfirmed, RunFinished, RunStarted
from backup_system.common.ids import new_command_id
from backup_system.manager.application import ManagerApplication
from backup_system.manager.database import open_manager_database
from backup_system.manager.executor_process import ExecutorProcessResult
from backup_system.manager.layout import RuntimeLayout, initialize_data_layout
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
        assert not tuple(layout.commands_incoming.iterdir())
        assert (layout.commands_completed / f"{command.command_id}.json").is_file()
    finally:
        connection.close()
