"""Deadline calculation, overrun persistence and notification planning."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from backup_system.common.config import ScheduleConfig
from backup_system.common.time import require_aware
from backup_system.manager.notifications import NotificationRepository


def deadline_for(schedule: ScheduleConfig, scheduled_at: datetime) -> datetime | None:
    if schedule.deadline is None:
        return None
    scheduled_utc = require_aware(scheduled_at).astimezone(UTC)
    timezone = ZoneInfo(schedule.timezone)
    local = scheduled_utc.astimezone(timezone)
    hour, minute = (int(part) for part in schedule.deadline.split(":"))
    candidate_date = local.date()
    while True:
        candidate = _resolve_local(candidate_date, time(hour, minute), timezone)
        if candidate > scheduled_utc:
            return candidate
        candidate_date += timedelta(days=1)


def _resolve_local(value_date: date, value_time: time, timezone: ZoneInfo) -> datetime:
    naive = datetime.combine(value_date, value_time)
    while True:
        resolved = naive.replace(tzinfo=timezone, fold=0)
        round_trip = resolved.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
        if round_trip == naive:
            return resolved.astimezone(UTC)
        naive += timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class DeadlineSweep:
    active_alerts: int
    completion_summaries: int


class DeadlineMonitor:
    def __init__(
        self, connection: sqlite3.Connection, notifications: NotificationRepository
    ) -> None:
        self._connection = connection
        self._notifications = notifications

    def assign(self, run_id: UUID, deadline_at: datetime | None) -> None:
        value = require_aware(deadline_at).astimezone(UTC).isoformat() if deadline_at else None
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE runs SET deadline_at = ? WHERE run_id = ? AND state = 'running'",
                (value, str(run_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("deadline can be assigned only to a running run")

    def sweep(self, *, now: datetime) -> DeadlineSweep:
        timestamp = require_aware(now).astimezone(UTC)
        active = 0
        completed = 0
        with self._connection:
            rows = self._connection.execute(
                """SELECT run_id, job_id, stage, started_at, deadline_at
                FROM runs WHERE state = 'running' AND deadline_at < ?
                AND deadline_exceeded_at IS NULL""",
                (timestamp.isoformat(),),
            ).fetchall()
            for row in rows:
                run_id = UUID(str(row[0]))
                started_at = datetime.fromisoformat(str(row[3]))
                deadline_at = datetime.fromisoformat(str(row[4]))
                self._notifications.enqueue_in_transaction(
                    deduplication_key=f"deadline:{run_id}:initial",
                    run_id=run_id,
                    kind="deadline_overrun",
                    payload={
                        "job": str(row[1]),
                        "stage": str(row[2]) if row[2] is not None else None,
                        "elapsed_seconds": int((timestamp - started_at).total_seconds()),
                        "overrun_seconds": int((timestamp - deadline_at).total_seconds()),
                    },
                    created_at=timestamp,
                )
                self._connection.execute(
                    """UPDATE runs SET deadline_exceeded_at = ?, deadline_overrun_seconds = ?
                    WHERE run_id = ?""",
                    (
                        timestamp.isoformat(),
                        int((timestamp - deadline_at).total_seconds()),
                        str(run_id),
                    ),
                )
                active += 1

            rows = self._connection.execute(
                """SELECT run_id, job_id, deadline_overrun_seconds
                FROM runs WHERE state = 'finished' AND deadline_exceeded_at IS NOT NULL
                AND deadline_final_notified = 0"""
            ).fetchall()
            for row in rows:
                run_id = UUID(str(row[0]))
                self._notifications.enqueue_in_transaction(
                    deduplication_key=f"deadline:{run_id}:final",
                    run_id=run_id,
                    kind="deadline_overrun_finished",
                    payload={
                        "job": str(row[1]),
                        "overrun_seconds": int(row[2]),
                    },
                    created_at=timestamp,
                )
                self._connection.execute(
                    "UPDATE runs SET deadline_final_notified = 1 WHERE run_id = ?",
                    (str(run_id),),
                )
                completed += 1
        return DeadlineSweep(active, completed)
