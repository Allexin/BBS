import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from backup_system.manager.database import open_manager_database
from backup_system.manager.operations import OperationsRepository, RunResult
from backup_system.manager.projection_builder import ProjectionBuilder
from backup_system.manager.safety import SafetyLatchRepository
from backup_system.manager.smart_history import SmartHistoryRepository, SmartMetrics


def test_builder_projects_jobs_queue_disks_volumes_and_smart_trends(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    operations = OperationsRepository(connection)
    smart = SmartHistoryRepository(connection)
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        operations.upsert_job(
            job_id="data", display_name="Data", enabled=True, config_valid=True, updated_at=now
        )
        operations.enqueue(
            deduplication_key="data:finished",
            job_id="data",
            kind="backup",
            trigger_source="scheduled",
            queued_at=now - timedelta(hours=3),
        )
        finished_run = operations.claim_next(started_at=now - timedelta(hours=2))
        assert finished_run is not None
        operations.finish_run(
            finished_run.run_id,
            result=RunResult.SUCCESS,
            exit_code=0,
            disk_offline_confirmed=True,
            finished_at=now - timedelta(hours=1),
        )
        with connection:
            connection.execute(
                """INSERT INTO backup_metrics(
                    run_id, source_logical_bytes, protected_logical_bytes,
                    repository_added_bytes, observed_at
                ) VALUES (?, 1000, 900, 100, ?)""",
                (str(finished_run.run_id), now.isoformat()),
            )
        operations.enqueue(
            deduplication_key="data:queued",
            job_id="data",
            kind="check",
            trigger_source="manual",
            queued_at=now - timedelta(minutes=5),
        )

        for observed_at, pending in (
            (now - timedelta(days=31), 0),
            (now - timedelta(hours=25), 1),
            (now - timedelta(hours=1), 3),
        ):
            smart.record(
                disk_id="internal-disk-key",
                public_disk_id="disk-1",
                identity_key="SECRET-SERIAL",
                role="backup",
                observed_at=observed_at,
                operational_state="offline",
                smart_health="warning",
                metrics=SmartMetrics(pending_sectors=pending),
                model="Test Disk",
                media_type="HDD",
                bus_type="SATA",
                capacity_bytes=10000,
            )
        with connection:
            connection.execute(
                """INSERT INTO smart_test_results(
                    run_id, disk_id, identity_key, test_type, result, reason,
                    duration_seconds, remaining_percent, finished_at
                ) VALUES (?, 'internal-disk-key', ?, 'short', 'timeout',
                    'SMART self-test completion timed out', 900, 90, ?)""",
                (str(finished_run.run_id), "SECRET-SERIAL", now.isoformat()),
            )
            connection.execute(
                """INSERT INTO volumes(
                    volume_id, public_volume_id, disk_id, display_name, label,
                    filesystem, role, last_seen_at
                ) VALUES ('internal-volume', 'backup-volume', 'internal-disk-key',
                    'Backup volume', 'BBS', 'NTFS', 'backup', ?)""",
                (now.isoformat(),),
            )
            connection.execute(
                """INSERT INTO volume_observations(
                    volume_id, observed_at, online, total_bytes, free_bytes
                ) VALUES ('internal-volume', ?, 0, 10000, 2500)""",
                (now.isoformat(),),
            )

        status, health = ProjectionBuilder(
            connection,
            job_kinds={"data": "snapshot"},
            job_deadlines={"data": "08:00"},
            next_operations={"data": "check"},
        ).build(
            now=now,
            manager_started_at=now - timedelta(days=1),
            manager_state="idle",
            version="0.1.0",
        )

        assert status.generation_id == health.generation_id
        assert health.status_stale_after_seconds == 60
        assert status.backup_disk_state == "offline"
        assert status.jobs[0].kind == "snapshot"
        assert status.jobs[0].last_run is not None
        assert status.jobs[0].last_run.state == "queued"
        assert status.jobs[0].previous_run is not None
        assert status.jobs[0].previous_run.result == "success"
        assert status.jobs[0].backup_metrics is not None
        assert status.jobs[0].backup_metrics.repository_added_bytes == 100
        assert status.operations[0].state == "queued"
        metric = status.disks[0].metrics["pending_sectors"]
        assert (metric.current, metric.previous, metric.delta) == (3, 1, 2)
        assert metric.change_24h == 2
        assert metric.change_30d == 3
        assert metric.last_regression_at == now - timedelta(hours=1)
        assert status.disks[0].last_self_test is not None
        assert status.disks[0].last_self_test.result == "timeout"
        assert status.disks[0].last_self_test.remaining_percent == 90
        assert status.volumes[0].used_bytes == 7500
        assert status.volumes[0].free_percent == 25
        assert not status.volumes[0].stale

        public_json = json.dumps(status.model_dump(mode="json"))
        assert "SECRET-SERIAL" not in public_json
        assert "internal-disk-key" not in public_json
        assert "internal-volume" not in public_json
    finally:
        connection.close()


def test_builder_uses_unknown_instead_of_inventing_missing_data(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    try:
        OperationsRepository(connection).upsert_job(
            job_id="empty", display_name="Empty", enabled=True, config_valid=True
        )
        status, _ = ProjectionBuilder(connection).build(
            now=datetime(2026, 8, 28, tzinfo=UTC),
            manager_started_at=datetime(2026, 8, 28, tzinfo=UTC),
            manager_state="idle",
            version="0.1.0",
        )
        assert status.overall_health == "unknown"
        assert status.jobs[0].kind == "unknown"
        assert status.jobs[0].health == "unknown"
        assert status.jobs[0].last_run is None
    finally:
        connection.close()


def test_running_operation_exposes_job_id_and_progress_heartbeat(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        operations = OperationsRepository(connection)
        operations.upsert_job(
            job_id="data", display_name="Data", enabled=True, config_valid=True
        )
        operations.enqueue(
            deduplication_key="data:running",
            job_id="data",
            kind="backup",
            trigger_source="manual",
            queued_at=now - timedelta(minutes=2),
        )
        run = operations.claim_next(started_at=now - timedelta(minutes=1))
        assert run is not None
        operations.update_stage(run.run_id, "backing_up", changed_at=now)
        operations.update_progress(
            run.run_id,
            files_done=927,
            files_total=11551,
            bytes_done=11351492,
            bytes_total=422964384,
            updated_at=now,
        )
        status, _ = ProjectionBuilder(connection).build(
            now=now, manager_started_at=now, manager_state="running", version="test"
        )
        operation = status.operations[0]
        assert operation.job_id == "data"
        assert operation.stage == "backing_up"
        assert operation.progress is not None
        assert operation.progress.files_done == 927
        assert operation.progress.updated_at == now
    finally:
        connection.close()


def test_active_safety_latch_is_critical_and_explains_queued_work(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    now = datetime(2026, 8, 28, tzinfo=UTC)
    try:
        operations = OperationsRepository(connection)
        operations.upsert_job(
            job_id="data", display_name="Data", enabled=True, config_valid=True
        )
        operations.enqueue(
            deduplication_key="queued", job_id="data", kind="backup",
            trigger_source="manual", queued_at=now,
        )
        with connection:
            SafetyLatchRepository(connection).set_disk_lifecycle_in_transaction(
                job_id="data", source_run_id=UUID(int=9),
                reason="offline_not_confirmed", created_at=now,
            )

        status, _ = ProjectionBuilder(connection).build(
            now=now, manager_started_at=now, manager_state="idle", version="test"
        )

        assert status.overall_health == "critical"
        assert status.health_issues[-1].kind == "safety-latch"
        assert status.operations[0].blocked_reason is not None
    finally:
        connection.close()


def test_failed_self_test_overrides_healthy_passive_state(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    try:
        operations = OperationsRepository(connection)
        operations.upsert_job(
            job_id="smart", display_name="SMART", enabled=True, config_valid=True
        )
        operation = operations.enqueue(
            deduplication_key="smart:run", job_id="smart", kind="smart-test",
            trigger_source="manual", queued_at=now,
        )
        run = operations.claim_next(started_at=now)
        assert operation is not None and run is not None
        SmartHistoryRepository(connection).record(
            disk_id="system-disk-2", public_disk_id="ignored", identity_key="b" * 64,
            role="monitored", observed_at=now, operational_state="unknown",
            smart_health="healthy", metrics=SmartMetrics(overall_passed=True),
            manufacturer="Vendor", model="Model", mount_points=("D:\\",),
        )
        with connection:
            connection.execute(
                """INSERT INTO smart_test_results(
                    run_id, disk_id, identity_key, test_type, result, reason,
                    duration_seconds, remaining_percent, finished_at
                ) VALUES (?, 'system-disk-2', ?, 'short', 'timeout', 'timed out', 900, 90, ?)""",
                (str(run.run_id), "b" * 64, now.isoformat()),
            )
        operations.finish_run(
            run.run_id, result=RunResult.FAILED, exit_code=30,
            disk_offline_confirmed=True, finished_at=now,
        )
        status, _ = ProjectionBuilder(
            connection,
            job_kinds={"smart": "smart-test"},
            disk_health_policies={
                f"disk-{'b' * 12}": (False, "Accepted risk for temporary media")
            },
        ).build(
            now=now, manager_started_at=now, manager_state="idle", version="test"
        )
        assert status.disks[0].smart_health == "warning"
        assert status.disks[0].passive_smart_health == "healthy"
        assert status.disks[0].affects_system_health is False
        assert status.disks[0].health_policy_reason == "Accepted risk for temporary media"
        assert status.disks[0].mount_points == ("D:\\",)
        assert status.jobs[0].health == "healthy"
        assert status.overall_health == "healthy"
        assert status.health_issues == ()
    finally:
        connection.close()


def test_projection_collapses_legacy_and_discovery_ids_by_identity(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    try:
        history = SmartHistoryRepository(connection)
        history.record(
            disk_id="test-disk", public_disk_id="test-disk", identity_key="c" * 64,
            role="monitored", observed_at=now - timedelta(hours=1),
            operational_state="unknown", smart_health="healthy", metrics=SmartMetrics(),
        )
        normalized = connection.execute(
            "SELECT normalized_json FROM disk_observations WHERE disk_id = 'test-disk'"
        ).fetchone()[0]
        with connection:
            connection.execute(
                """INSERT INTO physical_disks(
                    disk_id, public_disk_id, model, media_type, bus_type, capacity_bytes,
                    role, last_seen_at, manufacturer, mount_points_json
                ) VALUES ('system-disk-2', 'system-disk-2', 'Current model', 'ssd',
                    'SATA', 1000, 'monitored', ?, 'Vendor', '["D:\\\\"]')""",
                (now.isoformat(),),
            )
            connection.execute(
                """INSERT INTO disk_observations(
                    disk_id, observed_at, operational_state, smart_health, normalized_json
                ) VALUES ('system-disk-2', ?, 'unknown', 'healthy', ?)""",
                (now.isoformat(), normalized),
            )
        status, _ = ProjectionBuilder(connection).build(
            now=now, manager_started_at=now, manager_state="idle", version="test"
        )
        assert len(status.disks) == 1
        assert status.disks[0].disk_id == f"disk-{'c' * 12}"
        assert status.disks[0].model == "Current model"
    finally:
        connection.close()


def test_stale_smart_observation_is_visible_but_warns(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    try:
        SmartHistoryRepository(connection).record(
            disk_id="old-disk", public_disk_id="ignored", identity_key="d" * 64,
            role="monitored", observed_at=now - timedelta(hours=49),
            operational_state="unknown", smart_health="healthy",
            metrics=SmartMetrics(overall_passed=True), model="Old disk",
        )
        status, _ = ProjectionBuilder(
            connection, smart_stale_after_hours=48
        ).build(
            now=now, manager_started_at=now, manager_state="idle", version="test"
        )
        assert status.disks[0].stale
        assert status.disks[0].passive_smart_health == "unknown"
        assert status.disks[0].smart_health == "warning"
        assert status.disks[0].health_reasons == ("SMART observation is stale",)
        assert status.overall_health == "warning"
    finally:
        connection.close()
