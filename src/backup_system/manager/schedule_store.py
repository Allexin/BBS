"""Transactional persistence and queue integration for scheduler decisions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from backup_system.common.config import CycleItem, ScheduleConfig
from backup_system.common.time import require_aware
from backup_system.manager.operations import EnqueueResult, OperationsRepository
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
    def __init__(self, connection: sqlite3.Connection, operations: OperationsRepository) -> None:
        self._connection = connection
        self._operations = operations

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

    def _required_state(self, job_id: str) -> tuple[Any, ...]:
        row = self._connection.execute(
            """SELECT slot_counter, recovery_check_required, next_fire_at
            FROM schedule_state WHERE job_id = ?""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"schedule state is missing for {job_id}")
        return cast(tuple[Any, ...], row)
