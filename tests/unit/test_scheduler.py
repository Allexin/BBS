from datetime import UTC, datetime, timedelta

from backup_system.common.config import ScheduleConfig
from backup_system.manager.scheduler import (
    ScheduleCursor,
    advance_cycle,
    evaluate_poll,
    evaluate_startup,
    next_cron_fire,
    phase_for,
)


def _schedule() -> ScheduleConfig:
    return ScheduleConfig.model_validate(
        {
            "cron": "0 0 * * 1",
            "timezone": "Europe/Samara",
            "deadline": "08:00",
            "cycle": [
                {"operation": "backup"},
                {"operation": "backup"},
                {"operation": "check", "mode": "subset"},
            ],
        }
    )


def test_cron_is_calculated_in_configured_timezone() -> None:
    fire = next_cron_fire(_schedule(), datetime(2026, 8, 30, 20, 0, tzinfo=UTC))
    assert fire == datetime(2026, 8, 30, 20, 0, tzinfo=UTC) + timedelta(days=7)


def test_dst_nonexistent_time_is_skipped_and_ambiguous_time_fires_once() -> None:
    schedule = ScheduleConfig.model_validate(
        {
            "cron": "30 2 * * *",
            "timezone": "Europe/Berlin",
            "deadline": "08:00",
            "cycle": [{"operation": "backup"}],
        }
    )
    spring = next_cron_fire(schedule, datetime(2026, 3, 28, 12, tzinfo=UTC))
    assert spring == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)

    first_fold = next_cron_fire(schedule, datetime(2026, 10, 24, 12, tzinfo=UTC))
    after_first_fold = next_cron_fire(schedule, first_fold)
    assert first_fold == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert after_first_fold == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)


def test_startup_records_missed_without_catchup() -> None:
    schedule = _schedule()
    stored = datetime(2026, 8, 2, 20, 0, tzinfo=UTC)
    evaluation = evaluate_startup(
        schedule, stored_next_fire=stored, now=datetime(2026, 8, 20, tzinfo=UTC)
    )
    assert evaluation.due_at is None
    assert len(evaluation.missed_at) == 3
    assert evaluation.next_fire_at > datetime(2026, 8, 20, tzinfo=UTC)


def test_poll_accepts_only_current_interval() -> None:
    schedule = _schedule()
    stored = datetime(2026, 8, 30, 20, 0, tzinfo=UTC)
    on_time = evaluate_poll(
        schedule,
        stored_next_fire=stored,
        now=datetime(2026, 8, 30, 20, 0, 4, tzinfo=UTC),
        poll_seconds=5,
    )
    late = evaluate_poll(
        schedule,
        stored_next_fire=stored,
        now=datetime(2026, 8, 30, 20, 1, tzinfo=UTC),
        poll_seconds=5,
    )
    assert on_time.due_at == stored and on_time.missed_at == ()
    assert late.due_at is None and late.missed_at == (stored,)


def test_cycle_advances_only_for_allowed_successes() -> None:
    schedule = _schedule()
    cursor = ScheduleCursor(0)
    assert phase_for(schedule, cursor).operation == "backup"
    assert (
        advance_cycle(
            schedule,
            cursor,
            result="failed",
            trigger_source="scheduled",
            operation="backup",
        )
        == cursor
    )
    cursor = advance_cycle(
        schedule,
        cursor,
        result="success",
        trigger_source="scheduled",
        operation="backup",
    )
    assert cursor.slot_counter == 1


def test_manual_full_check_can_advance_check_or_clear_recovery() -> None:
    schedule = _schedule()
    check_cursor = ScheduleCursor(2)
    advanced = advance_cycle(
        schedule,
        check_cursor,
        result="success",
        trigger_source="manual",
        operation="check",
        check_mode="full",
    )
    assert advanced.slot_counter == 3

    recovery = ScheduleCursor(0, recovery_check_required=True)
    cleared = advance_cycle(
        schedule,
        recovery,
        result="warning",
        trigger_source="manual",
        operation="check",
        check_mode="full",
    )
    assert not cleared.recovery_check_required and cleared.slot_counter == 0
