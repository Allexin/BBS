"""Manager SQLite connection policy and forward-only schema migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backup_system.common.time import utc_now

LATEST_SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    config_valid INTEGER NOT NULL,
    config_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE schedule_state (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    slot_counter INTEGER NOT NULL,
    recovery_check_required INTEGER NOT NULL DEFAULT 0,
    last_evaluated_at TEXT NOT NULL,
    next_fire_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE safety_latches (
    latch_key TEXT PRIMARY KEY,
    latch_type TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    source_run_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cleared_at TEXT
);

CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    deduplication_key TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    kind TEXT NOT NULL,
    mode TEXT,
    request_json TEXT,
    trigger_source TEXT NOT NULL,
    scheduled_at TEXT,
    queued_at TEXT NOT NULL,
    state TEXT NOT NULL,
    removed_at TEXT,
    terminal_reason TEXT
);

CREATE UNIQUE INDEX uq_operations_unfinished_job_kind
ON operations(job_id, kind)
WHERE state IN ('queued', 'running');

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    stage TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    snapshot_id TEXT,
    stage_started_at TEXT,
    progress_updated_at TEXT,
    files_done INTEGER,
    files_total INTEGER,
    bytes_done INTEGER,
    bytes_total INTEGER,
    bytes_added INTEGER,
    warning_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    disk_offline_confirmed INTEGER NOT NULL DEFAULT 0,
    diagnostics_log_date_from TEXT,
    diagnostics_log_date_to TEXT
);

CREATE TABLE run_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    emitted_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE notifications (
    notification_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(run_id),
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE physical_disks (
    disk_id TEXT PRIMARY KEY,
    public_disk_id TEXT NOT NULL UNIQUE,
    model TEXT,
    media_type TEXT,
    bus_type TEXT,
    capacity_bytes INTEGER,
    role TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE disk_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    disk_id TEXT NOT NULL REFERENCES physical_disks(disk_id),
    observed_at TEXT NOT NULL,
    operational_state TEXT NOT NULL,
    smart_health TEXT NOT NULL,
    temperature_celsius INTEGER,
    power_on_hours INTEGER,
    reallocated_sectors INTEGER,
    pending_sectors INTEGER,
    offline_uncorrectable INTEGER,
    interface_crc_errors INTEGER,
    nvme_percentage_used INTEGER,
    nvme_media_errors INTEGER,
    normalized_json TEXT NOT NULL
);

CREATE TABLE volumes (
    volume_id TEXT PRIMARY KEY,
    public_volume_id TEXT NOT NULL UNIQUE,
    disk_id TEXT NOT NULL REFERENCES physical_disks(disk_id),
    display_name TEXT,
    label TEXT,
    filesystem TEXT,
    role TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE volume_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id TEXT NOT NULL REFERENCES volumes(volume_id),
    observed_at TEXT NOT NULL,
    online INTEGER NOT NULL,
    total_bytes INTEGER,
    free_bytes INTEGER
);

CREATE TABLE backup_metrics (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    source_logical_bytes INTEGER,
    protected_logical_bytes INTEGER,
    retained_logical_bytes INTEGER,
    bytes_read INTEGER,
    bytes_written INTEGER,
    repository_added_bytes INTEGER,
    repository_physical_bytes INTEGER,
    repository_free_bytes INTEGER,
    observed_at TEXT NOT NULL
);
"""


class SchemaVersionError(RuntimeError):
    """The database schema is newer than this application understands."""


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")


def _execute_script_atomically(connection: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete migration statement")


def _migrate(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        versions = {
            int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")
        }
        if any(version > LATEST_SCHEMA_VERSION for version in versions):
            raise SchemaVersionError("database schema is newer than this application")
        if 1 not in versions:
            _execute_script_atomically(connection, _SCHEMA_V1)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (1, utc_now().isoformat()),
            )


def open_manager_database(path: Path) -> sqlite3.Connection:
    """Open, configure and migrate the manager database at an existing state path."""
    connection = sqlite3.connect(path)
    try:
        _configure(connection)
        _migrate(connection)
    except BaseException:
        connection.close()
        raise
    return connection
