"""Transactional persistence and queue integration for scheduler decisions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from backup_system.common.config import CycleItem, ScheduleConfig
from backup_system.common.time import require_aware
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import (
    EnqueueDisposition,
    EnqueueResult,
    OperationsRepository,
    OperationState,
)
from backup_system.manager.scheduler import (
    CronEvaluation,
    ScheduleCursor,
    TerminalResult,
    TriggerSource,
    advance_cycle,
    evaluate_poll,
    evaluate_startup,
    next_cron_fire,
    phase_for,
)
from backup_system.manager.scheduler_events import SchedulerEventRepository


@dataclass(frozen=True, slots=True)
class StartupScheduleResult:
    state_was_missing: bool
    missed_at: tuple[datetime, ...]
    phase: CycleItem
    next_fire_at: datetime


@dataclass(frozen=True, slots=True)
class PollScheduleResult:
    evaluation: CronEvaluation
    phase: CycleItem
    enqueue_result: EnqueueResult | None


class ScheduleStore:
    def __init__(
        self,
        connection: sqlite3.Connection,
        operations: OperationsRepository,
        events: SchedulerEventRepository | None = None,
        notifications: NotificationRepository | None = None,
    ) -> None:
        self._connection = connection
        self._operations = operations
        self._events = events
        self._notifications = notifications

    def initialize_new_job(self, job_id: str, schedule: ScheduleConfig, *, now: datetime) -> None:
        timestamp = require_aware(now)
        next_fire = next_cron_fire(schedule, timestamp)
        with self._connection:
            self._connection.execute(
                """INSERT INTO schedule_state(
                    job_id, slot_counter, recovery_check_required,
                    last_evaluated_at, next_fire_at, updated_at
                ) VALUES (?, 0, 0, ?, ?, ?)
                ON CONFLICT(job_id) DO NOTHING""",
                (job_id, timestamp.isoformat(), next_fire.isoformat(), timestamp.isoformat()),
            )

    def reconcile_startup(
        self, job_id: str, schedule: ScheduleConfig, *, now: datetime
    ) -> StartupScheduleResult:
        timestamp = require_aware(now)
        with self._connection:
            row = self._connection.execute(
                """SELECT slot_counter, recovery_check_required, next_fire_at
                FROM schedule_state WHERE job_id = ?""",
                (job_id,),
            ).fetchone()
            if row is None:
                cursor = ScheduleCursor(0, recovery_check_required=True)
                next_fire = next_cron_fire(schedule, timestamp)
                self._connection.execute(
                    """INSERT INTO schedule_state(
                        job_id, slot_counter, recovery_check_required,
                        last_evaluated_at, next_fire_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        cursor.slot_counter,
                        int(cursor.recovery_check_required),
                        timestamp.isoformat(),
                        next_fire.isoformat(),
                        timestamp.isoformat(),
                    ),
                )
                return StartupScheduleResult(True, (), phase_for(schedule, cursor), next_fire)
            cursor = ScheduleCursor(int(row[0]), bool(row[1]))
            evaluation = evaluate_startup(
                schedule,
                stored_next_fire=datetime.fromisoformat(str(row[2])),
                now=timestamp,
            )
            self._record_missed(
                job_id,
                phase_for(schedule, cursor),
                evaluation,
                timestamp,
                reason="manager_downtime",
            )
            self._connection.execute(
                """UPDATE schedule_state SET
                    last_evaluated_at = ?, next_fire_at = ?, updated_at = ?
                WHERE job_id = ?""",
                (
                    timestamp.isoformat(),
                    evaluation.next_fire_at.isoformat(),
                    timestamp.isoformat(),
                    job_id,
                ),
            )
        return StartupScheduleResult(
            False, evaluation.missed_at, phase_for(schedule, cursor), evaluation.next_fire_at
        )

    def poll(
        self,
        job_id: str,
        schedule: ScheduleConfig,
        *,
        now: datetime,
        poll_seconds: int,
    ) -> PollScheduleResult:
        timestamp = require_aware(now)
        with self._connection:
            row = self._required_state(job_id)
            cursor = ScheduleCursor(int(row[0]), bool(row[1]))
            phase = phase_for(schedule, cursor)
            evaluation = evaluate_poll(
                schedule,
                stored_next_fire=datetime.fromisoformat(str(row[2])),
                now=timestamp,
                poll_seconds=poll_seconds,
            )
            self._record_missed(job_id, phase, evaluation, timestamp, reason="poll_late")
            enqueue_result = None
            if evaluation.due_at is not None:
                enqueue_result = self._operations.enqueue_in_transaction(
                    deduplication_key=f"schedule:{job_id}:{evaluation.due_at.isoformat()}",
                    job_id=job_id,
                    kind=phase.operation,
                    mode=phase.mode,
                    trigger_source="scheduled",
                    scheduled_at=evaluation.due_at,
                    queued_at=timestamp,
                )
                self._record_trigger_outcome(
                    job_id, phase, evaluation.due_at, enqueue_result, timestamp
                )
            self._connection.execute(
                """UPDATE schedule_state SET
                    last_evaluated_at = ?, next_fire_at = ?, updated_at = ?
                WHERE job_id = ?""",
                (
                    timestamp.isoformat(),
                    evaluation.next_fire_at.isoformat(),
                    timestamp.isoformat(),
                    job_id,
                ),
            )
        return PollScheduleResult(evaluation, phase, enqueue_result)

    def record_completion(
        self,
        job_id: str,
        schedule: ScheduleConfig,
        *,
        result: TerminalResult,
        trigger_source: TriggerSource,
        operation: str,
        check_mode: str | None,
        completed_at: datetime,
    ) -> ScheduleCursor:
        timestamp = require_aware(completed_at)
        with self._connection:
            row = self._required_state(job_id)
            cursor = ScheduleCursor(int(row[0]), bool(row[1]))
            updated = advance_cycle(
                schedule,
                cursor,
                result=result,
                trigger_source=trigger_source,
                operation=operation,
                check_mode=check_mode,
            )
            self._connection.execute(
                """UPDATE schedule_state SET
                    slot_counter = ?, recovery_check_required = ?, updated_at = ?
                WHERE job_id = ?""",
                (
                    updated.slot_counter,
                    int(updated.recovery_check_required),
                    timestamp.isoformat(),
                    job_id,
                ),
            )
        return updated

    def _record_missed(
        self,
        job_id: str,
        phase: CycleItem,
        evaluation: CronEvaluation,
        created_at: datetime,
        reason: str,
    ) -> None:
        if self._events is None:
            return
        for missed_at in evaluation.missed_at:
            self._events.append_in_transaction(
                deduplication_key=f"schedule-missed:{job_id}:{missed_at.isoformat()}",
                event_type="schedule_missed",
                job_id=job_id,
                operation_kind=phase.operation,
                scheduled_at=missed_at,
                reason=reason,
                payload={"mode": phase.mode},
                created_at=created_at,
            )

    def _record_trigger_outcome(
        self,
        job_id: str,
        phase: CycleItem,
        scheduled_at: datetime,
        result: EnqueueResult,
        created_at: datetime,
    ) -> None:
        if result.disposition is EnqueueDisposition.COALESCED and self._events is not None:
            reason = (
                "already_running"
                if result.existing_state is OperationState.RUNNING
                else "already_queued"
            )
            self._events.append_in_transaction(
                deduplication_key=f"duplicate-trigger:{job_id}:{scheduled_at.isoformat()}",
                event_type="duplicate_trigger_skipped",
                job_id=job_id,
                operation_kind=phase.operation,
                scheduled_at=scheduled_at,
                reason=reason,
                created_at=created_at,
            )
            return
        if result.disposition is not EnqueueDisposition.CREATED:
            return
        running = self._connection.execute(
            """SELECT runs.run_id, runs.job_id, jobs.display_name, runs.stage,
                runs.started_at FROM runs JOIN jobs ON jobs.job_id = runs.job_id
            WHERE runs.state = 'running' LIMIT 1"""
        ).fetchone()
        if running is None or str(running[1]) == job_id:
            return
        queued_name_row = self._connection.execute(
            "SELECT display_name FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        queued_name = str(queued_name_row[0]) if queued_name_row else job_id
        elapsed = max(
            0,
            int((created_at - datetime.fromisoformat(str(running[4]))).total_seconds()),
        )
        if self._events is not None:
            self._events.append_in_transaction(
                deduplication_key=f"overlap:{job_id}:{scheduled_at.isoformat()}",
                event_type="schedule_overlap",
                job_id=job_id,
                operation_kind=phase.operation,
                scheduled_at=scheduled_at,
                payload={"running_run_id": str(running[0])},
                created_at=created_at,
            )
        if self._notifications is not None:
            self._notifications.enqueue_in_transaction(
                deduplication_key=f"overlap:{job_id}:{scheduled_at.isoformat()}",
                kind="schedule_overlap",
                payload={
                    "running_job": str(running[2]),
                    "running_stage": str(running[3]) if running[3] is not None else None,
                    "running_elapsed_seconds": elapsed,
                    "queued_job": queued_name,
                },
                created_at=created_at,
            )

    def _required_state(self, job_id: str) -> tuple[Any, ...]:
        row = self._connection.execute(
            """SELECT slot_counter, recovery_check_required, next_fire_at
            FROM schedule_state WHERE job_id = ?""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"schedule state is missing for {job_id}")
        return cast(tuple[Any, ...], row)
