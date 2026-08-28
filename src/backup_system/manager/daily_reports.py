"""No-catch-up daily heartbeat planning and durable report formation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from backup_system.common.config import CycleItem, ScheduleConfig
from backup_system.common.time import require_aware
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.scheduler import evaluate_poll, evaluate_startup, next_cron_fire

_REPORT_ID = "telegram-daily"


@dataclass(frozen=True, slots=True)
class DailyReportPoll:
    formed: bool
    next_fire_at: datetime


class DailyReportStore:
    def __init__(
        self, connection: sqlite3.Connection, notifications: NotificationRepository
    ) -> None:
        self._connection = connection
        self._notifications = notifications

    def initialize(self, *, cron: str, timezone: str, now: datetime) -> None:
        timestamp = _utc(now)
        schedule = _schedule(cron, timezone)
        next_fire = next_cron_fire(schedule, timestamp)
        with self._connection:
            self._connection.execute(
                """INSERT INTO daily_report_state(
                    report_id, last_formed_at, last_evaluated_at, next_fire_at, updated_at
                ) VALUES (?, ?, ?, ?, ?) ON CONFLICT(report_id) DO NOTHING""",
                (
                    _REPORT_ID,
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                    next_fire.isoformat(),
                    timestamp.isoformat(),
                ),
            )

    def reconcile_startup(self, *, cron: str, timezone: str, now: datetime) -> tuple[datetime, ...]:
        timestamp = _utc(now)
        schedule = _schedule(cron, timezone)
        self.initialize(cron=cron, timezone=timezone, now=timestamp)
        with self._connection:
            row = self._state()
            evaluation = evaluate_startup(
                schedule,
                stored_next_fire=datetime.fromisoformat(str(row[1])),
                now=timestamp,
            )
            self._connection.execute(
                """UPDATE daily_report_state SET last_evaluated_at = ?,
                    next_fire_at = ?, updated_at = ? WHERE report_id = ?""",
                (
                    timestamp.isoformat(),
                    evaluation.next_fire_at.isoformat(),
                    timestamp.isoformat(),
                    _REPORT_ID,
                ),
            )
        return evaluation.missed_at

    def poll(
        self,
        *,
        cron: str,
        timezone: str,
        health: str,
        now: datetime,
        poll_seconds: int,
    ) -> DailyReportPoll:
        timestamp = _utc(now)
        schedule = _schedule(cron, timezone)
        with self._connection:
            row = self._state()
            period_start = datetime.fromisoformat(str(row[0]))
            evaluation = evaluate_poll(
                schedule,
                stored_next_fire=datetime.fromisoformat(str(row[1])),
                now=timestamp,
                poll_seconds=poll_seconds,
            )
            due_at = evaluation.due_at
            formed = due_at is not None
            if due_at is not None:
                payload = self._payload(period_start, timestamp, health)
                self._notifications.enqueue_in_transaction(
                    deduplication_key=f"daily-report:{due_at.isoformat()}",
                    kind="daily_report",
                    payload=payload,
                    created_at=timestamp,
                )
                self._connection.execute(
                    """UPDATE daily_report_state SET last_formed_at = ?,
                        last_evaluated_at = ?, next_fire_at = ?, updated_at = ?
                    WHERE report_id = ?""",
                    (
                        timestamp.isoformat(),
                        timestamp.isoformat(),
                        evaluation.next_fire_at.isoformat(),
                        timestamp.isoformat(),
                        _REPORT_ID,
                    ),
                )
            else:
                self._connection.execute(
                    """UPDATE daily_report_state SET last_evaluated_at = ?,
                        next_fire_at = ?, updated_at = ? WHERE report_id = ?""",
                    (
                        timestamp.isoformat(),
                        evaluation.next_fire_at.isoformat(),
                        timestamp.isoformat(),
                        _REPORT_ID,
                    ),
                )
        return DailyReportPoll(formed, evaluation.next_fire_at)

    def _payload(
        self, period_start: datetime, period_end: datetime, health: str
    ) -> dict[str, object]:
        rows = self._connection.execute(
            """SELECT jobs.display_name, runs.kind, runs.result, runs.started_at,
                runs.finished_at, runs.error_count
            FROM runs JOIN jobs ON jobs.job_id = runs.job_id
            WHERE runs.state = 'finished' AND runs.finished_at > ? AND runs.finished_at <= ?
            ORDER BY runs.finished_at""",
            (period_start.isoformat(), period_end.isoformat()),
        ).fetchall()
        backups: list[str] = []
        errors: list[str] = []
        for name, kind, result, started_at, finished_at, error_count in rows:
            duration = int(
                (
                    datetime.fromisoformat(str(finished_at))
                    - datetime.fromisoformat(str(started_at))
                ).total_seconds()
            )
            summary = f"{name}: {result}, {duration}s"
            if str(kind) == "backup":
                backups.append(summary)
            if str(result) in {"failed", "cancelled", "interrupted"} or int(error_count) > 0:
                errors.append(f"{name} {kind}: {result}")
        return {
            "period_from": period_start.isoformat(),
            "period_to": period_end.isoformat(),
            "health": health,
            "backups": backups,
            "errors": errors,
        }

    def _state(self) -> tuple[object, ...]:
        row = self._connection.execute(
            """SELECT last_formed_at, next_fire_at FROM daily_report_state
            WHERE report_id = ?""",
            (_REPORT_ID,),
        ).fetchone()
        if row is None:
            raise RuntimeError("daily report state is missing")
        return tuple(row)


def _schedule(cron: str, timezone: str) -> ScheduleConfig:
    return ScheduleConfig(
        cron=cron,
        timezone=timezone,
        cycle=(CycleItem(operation="backup"),),
    )


def _utc(value: datetime) -> datetime:
    return require_aware(value).astimezone(UTC)
