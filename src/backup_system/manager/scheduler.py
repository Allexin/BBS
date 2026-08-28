"""Deterministic cron and job-cycle decisions with no catch-up."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from croniter import croniter

from backup_system.common.config import CycleItem, ScheduleConfig
from backup_system.common.time import require_aware

TerminalResult = Literal["success", "warning", "failed", "cancelled", "interrupted"]
TriggerSource = Literal["scheduled", "manual"]


@dataclass(frozen=True, slots=True)
class ScheduleCursor:
    slot_counter: int
    recovery_check_required: bool = False

    def __post_init__(self) -> None:
        if self.slot_counter < 0:
            raise ValueError("slot_counter cannot be negative")


@dataclass(frozen=True, slots=True)
class CronEvaluation:
    due_at: datetime | None
    missed_at: tuple[datetime, ...]
    next_fire_at: datetime


def phase_for(schedule: ScheduleConfig, cursor: ScheduleCursor) -> CycleItem:
    if cursor.recovery_check_required:
        return CycleItem(operation="check", mode="full")
    return schedule.cycle[cursor.slot_counter % len(schedule.cycle)]


def next_cron_fire(schedule: ScheduleConfig, after: datetime) -> datetime:
    after_utc = require_aware(after).astimezone(UTC)
    timezone = ZoneInfo(schedule.timezone)
    local_naive = after_utc.astimezone(timezone).replace(tzinfo=None)
    iterator = croniter(schedule.cron, local_naive)
    while True:
        candidate = iterator.get_next(datetime)
        resolved = candidate.replace(tzinfo=timezone, fold=0)
        round_trip = resolved.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
        if round_trip != candidate:
            continue
        result = resolved.astimezone(UTC)
        if result > after_utc:
            return result


def evaluate_startup(
    schedule: ScheduleConfig, *, stored_next_fire: datetime, now: datetime
) -> CronEvaluation:
    return _evaluate(schedule, stored_next_fire=stored_next_fire, now=now, grace=None)


def evaluate_poll(
    schedule: ScheduleConfig,
    *,
    stored_next_fire: datetime,
    now: datetime,
    poll_seconds: int,
) -> CronEvaluation:
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    return _evaluate(
        schedule,
        stored_next_fire=stored_next_fire,
        now=now,
        grace=timedelta(seconds=poll_seconds),
    )


def _evaluate(
    schedule: ScheduleConfig,
    *,
    stored_next_fire: datetime,
    now: datetime,
    grace: timedelta | None,
) -> CronEvaluation:
    fire = require_aware(stored_next_fire).astimezone(UTC)
    now = require_aware(now).astimezone(UTC)
    if fire > now:
        return CronEvaluation(None, (), fire)
    elapsed: list[datetime] = []
    while fire <= now:
        elapsed.append(fire)
        fire = next_cron_fire(schedule, fire)
    due = None
    if grace is not None and now - elapsed[-1] <= grace:
        due = elapsed.pop()
    return CronEvaluation(due, tuple(elapsed), fire)


def advance_cycle(
    schedule: ScheduleConfig,
    cursor: ScheduleCursor,
    *,
    result: TerminalResult,
    trigger_source: TriggerSource,
    operation: str,
    check_mode: str | None = None,
) -> ScheduleCursor:
    if result not in {"success", "warning"}:
        return cursor
    current = phase_for(schedule, cursor)
    if cursor.recovery_check_required:
        if operation == "check" and check_mode == "full":
            return ScheduleCursor(slot_counter=cursor.slot_counter, recovery_check_required=False)
        return cursor
    if trigger_source == "scheduled":
        if operation == current.operation:
            return ScheduleCursor(slot_counter=cursor.slot_counter + 1)
        return cursor
    if (
        operation == "check"
        and current.operation == "check"
        and check_mode in {current.mode, "full"}
    ):
        return ScheduleCursor(slot_counter=cursor.slot_counter + 1)
    return cursor
