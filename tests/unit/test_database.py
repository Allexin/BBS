import sqlite3
from pathlib import Path

import pytest

from backup_system.manager.database import SchemaVersionError, open_manager_database

EXPECTED_TABLES = {
    "backup_metrics",
    "disk_observations",
    "daily_report_state",
    "jobs",
    "notifications",
    "operations",
    "physical_disks",
    "run_events",
    "runs",
    "safety_latches",
    "schedule_state",
    "schema_migrations",
    "sqlite_sequence",
    "volume_observations",
    "volumes",
}


def test_database_is_configured_and_migrated_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "manager.sqlite3"
    with open_manager_database(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        assert tables == EXPECTED_TABLES
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
        ]

    with open_manager_database(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (4,)


def test_foreign_keys_and_unfinished_operation_uniqueness_are_enforced(tmp_path: Path) -> None:
    with open_manager_database(tmp_path / "manager.sqlite3") as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO operations(
                    operation_id, deduplication_key, job_id, kind, trigger_source, queued_at, state
                ) VALUES ('op', 'dedup', 'missing', 'backup', 'manual', 'now', 'queued')"""
            )
        connection.execute(
            """INSERT INTO jobs(job_id, display_name, enabled, config_valid, updated_at)
            VALUES ('data', 'Data', 1, 1, 'now')"""
        )
        connection.execute(
            """INSERT INTO operations(
                operation_id, deduplication_key, job_id, kind, trigger_source, queued_at, state
            ) VALUES ('op-1', 'dedup-1', 'data', 'backup', 'manual', 'now', 'queued')"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO operations(
                    operation_id, deduplication_key, job_id, kind, trigger_source, queued_at, state
                ) VALUES ('op-2', 'dedup-2', 'data', 'backup', 'manual', 'now', 'running')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO notifications(
                    notification_id, deduplication_key, kind, payload_json, state, created_at
                ) VALUES ('notification', NULL, 'alert', '{}', 'pending', 'now')"""
            )


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "manager.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO schema_migrations VALUES (5, 'future')")
    connection.commit()
    connection.close()

    with pytest.raises(SchemaVersionError, match="newer"):
        open_manager_database(path)
