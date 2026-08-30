"""Long-lived manager service lifecycle and cooperative shutdown ordering."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from backup_system.common.config import ManagerConfig
from backup_system.common.config_io import validate_job_with_owner
from backup_system.manager.application import ManagerApplication
from backup_system.manager.database import open_manager_database
from backup_system.manager.layout import RuntimeLayout, initialize_data_layout
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import OperationsRepository

AsyncCallback = Callable[[], Awaitable[None]]


class ServiceLifecycle:
    def __init__(
        self,
        *,
        operations: OperationsRepository,
        stop_accepting: Callable[[], None],
        cancel_executor: AsyncCallback,
        wait_executor: AsyncCallback,
        publish_final_status: AsyncCallback,
    ) -> None:
        self._operations = operations
        self._stop_accepting = stop_accepting
        self._cancel_executor = cancel_executor
        self._wait_executor = wait_executor
        self._publish_final_status = publish_final_status
        self._stop_requested = asyncio.Event()
        self._shutdown_lock = asyncio.Lock()

    @property
    def stopping(self) -> bool:
        return self._stop_requested.is_set()

    def request_stop(self) -> None:
        self._stop_requested.set()

    async def wait_for_stop(self) -> None:
        await self._stop_requested.wait()

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            self._stop_accepting()
            self._operations.discard_queued_for_service_stop()
            await self._cancel_executor()
            await self._wait_executor()
            await self._publish_final_status()


async def run_service(config_path: Path, config: ManagerConfig) -> None:
    root = config_path.resolve(strict=False).parents[2]
    layout = RuntimeLayout(root)
    initialize_data_layout(layout)
    connection = open_manager_database(layout.database)
    try:
        operations = OperationsRepository(connection, NotificationRepository(connection))
        for job in config.jobs:
            operations.upsert_job(
                job_id=job.id,
                display_name=job.display_name,
                enabled=job.enabled,
                config_valid=True,
            )
        operations.reconcile_startup()

        application = ManagerApplication(
            layout=layout,
            config=config,
            operations=operations,
            job_kinds={
                job.id: validate_job_with_owner(layout.config, job.id).kind
                for job in config.jobs
            },
        )
        application.initialize()
        await application.publish("starting")

        lifecycle = ServiceLifecycle(
            operations=operations,
            stop_accepting=application.stop_accepting,
            cancel_executor=application.cancel_executor,
            wait_executor=application.wait_executor,
            publish_final_status=lambda: application.publish("stopping"),
        )
        _install_console_stop(lifecycle)
        worker = asyncio.create_task(
            _run_manager_loop(application, poll_seconds=config.scheduler.poll_seconds)
        )
        await lifecycle.wait_for_stop()
        await lifecycle.shutdown()
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    finally:
        connection.close()


def _install_console_stop(lifecycle: ServiceLifecycle) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        loop.call_soon_threadsafe(lifecycle.request_stop)

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)


async def _run_manager_loop(
    application: ManagerApplication, *, poll_seconds: int
) -> None:
    while application.accepting:
        executed = await application.run_iteration()
        await application.publish("running" if executed else "idle")
        if not executed:
            await asyncio.sleep(poll_seconds)
