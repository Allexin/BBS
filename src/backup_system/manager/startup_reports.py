"""Aggregate startup diagnostics and unreported missed schedules into one alert."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from backup_system.common.time import require_aware
from backup_system.manager.notifications import NotificationRepository


class StartupReportPlanner:
    def __init__(
        self, connection: sqlite3.Connection, notifications: NotificationRepository
    ) -> None:
        self._connection = connection
        self._notifications = notifications

    def enqueue(
        self,
        *,
        started_at: datetime,
        previous_seen_at: datetime,
        interrupted: tuple[str, ...] = (),
        disk_issues: tuple[str, ...] = (),
    ) -> tuple[object, bool]:
        started = _utc(started_at)
        previous = _utc(previous_seen_at)
        if previous > started:
            raise ValueError("previous manager timestamp cannot be after startup")
        with self._connection:
            rows = self._connection.execute(
                """SELECT scheduler_events.event_id, jobs.display_name,
                    scheduler_events.operation_kind, scheduler_events.scheduled_at
                FROM scheduler_events
                JOIN jobs ON jobs.job_id = scheduler_events.job_id
                WHERE scheduler_events.event_type = 'schedule_missed'
                  AND scheduler_events.reason = 'manager_downtime'
                  AND scheduler_events.startup_reported_at IS NULL
                ORDER BY scheduler_events.scheduled_at, jobs.job_id"""
            ).fetchall()
            backups: list[str] = []
            checks: list[str] = []
            other_count = 0
            event_ids: list[str] = []
            for event_id, display_name, operation_kind, scheduled_at in rows:
                event_ids.append(str(event_id))
                item = f"{display_name} at {scheduled_at}"
                if str(operation_kind) == "backup":
                    backups.append(item)
                elif str(operation_kind) == "check":
                    checks.append(item)
                else:
                    other_count += 1
            notification_id, created = self._notifications.enqueue_in_transaction(
                deduplication_key=f"startup-report:{started.isoformat()}",
                kind="startup_report",
                payload={
                    "downtime_seconds": int((started - previous).total_seconds()),
                    "interrupted": list(interrupted),
                    "disk_issues": list(disk_issues),
                    "missed_backups": backups,
                    "missed_checks": checks,
                    "missed_other_count": other_count,
                },
                created_at=started,
            )
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                self._connection.execute(
                    f"""UPDATE scheduler_events SET startup_reported_at = ?
                    WHERE event_id IN ({placeholders})""",
                    (started.isoformat(), *event_ids),
                )
        return notification_id, created


def _utc(value: datetime) -> datetime:
    return require_aware(value).astimezone(UTC)
