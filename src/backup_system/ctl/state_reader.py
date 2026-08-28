"""Read-only local SQLite projections for backupctl."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class LocalStateReader:
    def __init__(self, database: Path) -> None:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only = ON")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> LocalStateReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        active = self._connection.execute(
            """SELECT o.operation_id, o.job_id, o.kind, r.run_id, r.stage, r.started_at
            FROM operations AS o
            JOIN runs AS r ON r.operation_id = o.operation_id
            WHERE o.state = 'running' AND r.state = 'running'
            LIMIT 1"""
        ).fetchone()
        queued_count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM operations WHERE state = 'queued'"
            ).fetchone()[0]
        )
        return {
            "schema_version": 1,
            "active": dict(active) if active is not None else None,
            "queued_count": queued_count,
        }

    def jobs(self) -> dict[str, Any]:
        rows = self._connection.execute(
            """SELECT job_id, display_name, enabled, config_valid, config_error, updated_at
            FROM jobs ORDER BY job_id"""
        ).fetchall()
        return {
            "schema_version": 1,
            "jobs": [
                {
                    **dict(row),
                    "enabled": bool(row["enabled"]),
                    "config_valid": bool(row["config_valid"]),
                }
                for row in rows
            ],
        }

    def queue(self) -> dict[str, Any]:
        rows = self._connection.execute(
            """SELECT operation_id, job_id, kind, mode, trigger_source, queued_at, state
            FROM operations WHERE state IN ('running', 'queued')
            ORDER BY CASE state WHEN 'running' THEN 0 ELSE 1 END, queued_at, rowid"""
        ).fetchall()
        operations = []
        queued_position = 0
        for row in rows:
            value = dict(row)
            if row["state"] == "queued":
                queued_position += 1
                value["position"] = queued_position
            else:
                value["position"] = 0
            operations.append(value)
        return {"schema_version": 1, "operations": operations}
