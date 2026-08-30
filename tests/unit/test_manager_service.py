import asyncio
from pathlib import Path

from backup_system.manager.database import open_manager_database
from backup_system.manager.operations import OperationsRepository, OperationState
from backup_system.manager.service import ServiceLifecycle


def test_service_stop_discards_tail_before_cooperative_cleanup(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)
    operations.upsert_job(job_id="job-1", display_name="Job", enabled=True, config_valid=True)
    queued = operations.enqueue(
        deduplication_key="manual:1",
        job_id="job-1",
        kind="backup",
        trigger_source="manual",
    )
    calls: list[str] = []

    async def cancel() -> None:
        state = connection.execute(
            "SELECT state FROM operations WHERE operation_id = ?", (str(queued.operation_id),)
        ).fetchone()[0]
        assert state == OperationState.DISCARDED_ON_SERVICE_STOP
        calls.append("cancel")

    async def wait() -> None:
        calls.append("wait")

    async def publish() -> None:
        calls.append("publish")

    lifecycle = ServiceLifecycle(
        operations=operations,
        stop_accepting=lambda: calls.append("stop-accepting"),
        cancel_executor=cancel,
        wait_executor=wait,
        publish_final_status=publish,
    )
    asyncio.run(lifecycle.shutdown())
    assert calls == ["stop-accepting", "cancel", "wait", "publish"]
    connection.close()


def test_stop_request_wakes_service_waiter(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)

    async def scenario() -> None:
        async def noop() -> None:
            return None

        lifecycle = ServiceLifecycle(
            operations=operations,
            stop_accepting=lambda: None,
            cancel_executor=noop,
            wait_executor=noop,
            publish_final_status=noop,
        )
        lifecycle.request_stop()
        await lifecycle.wait_for_stop()
        assert lifecycle.stopping

    asyncio.run(scenario())
    connection.close()
