from uuid import uuid4

import pytest

from backup_system.manager.notification_format import render_notification
from backup_system.manager.notifications import PendingNotification


def _notification(kind: str, payload: dict[str, object]) -> PendingNotification:
    return PendingNotification(uuid4(), "key", None, kind, payload, 0)


def test_daily_report_explicitly_mentions_empty_sections() -> None:
    text = render_notification(_notification("daily_report", {"health": "healthy"}))
    assert "Health: healthy" in text
    assert "Backup jobs were not run" in text
    assert "No errors" in text


def test_startup_report_aggregates_missed_operations() -> None:
    text = render_notification(
        _notification(
            "startup_report",
            {
                "downtime_seconds": 3700,
                "interrupted": ["Data backup"],
                "missed_backups": ["Photos at 2026-08-28T00:00:00+04:00"],
                "missed_checks": [],
                "missed_other_count": 2,
            },
        )
    )
    assert "Manager downtime: 1h 1m" in text
    assert "Interrupted: Data backup" in text
    assert "Missed backup: Photos" in text
    assert "Missed maintenance operations: 2" in text


def test_deadline_and_smart_messages_are_bounded_structured_text() -> None:
    deadline = render_notification(
        _notification(
            "deadline_overrun",
            {"job": "Data", "stage": "backup", "elapsed_seconds": 3900, "overrun_seconds": 300},
        )
    )
    smart = render_notification(
        _notification(
            "smart_regression",
            {
                "disk": "Backup disk",
                "indicator": "pending_sectors",
                "previous": 0,
                "current": 1,
                "severity": "critical",
            },
        )
    )
    assert "Elapsed: 1h 5m" in deadline and "Overrun: 5m 0s" in deadline
    assert "Change: 0 -> 1" in smart


def test_absolute_smart_critical_message_is_supported() -> None:
    message = render_notification(
        _notification(
            "smart_critical_condition",
            {
                "disk": "disk-0123456789ab",
                "indicator": "pending_sectors",
                "current": 4,
                "severity": "critical",
            },
        )
    )
    assert "SMART critical condition: disk-0123456789ab" in message
    assert "Indicator: pending_sectors" in message
    assert "Current value: 4" in message


def test_unknown_notification_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        render_notification(_notification("unknown", {}))


def test_overlap_message_names_running_and_queued_jobs() -> None:
    text = render_notification(
        _notification(
            "schedule_overlap",
            {
                "running_job": "Photos",
                "running_stage": "backup",
                "running_elapsed_seconds": 600,
                "queued_job": "Data",
            },
        )
    )
    assert "Running: Photos" in text
    assert "Elapsed: 10m 0s" in text
    assert "Queued: Data" in text


def test_failure_message_contains_only_operational_summary() -> None:
    text = render_notification(
        _notification(
            "run_failed",
            {"job": "Data", "result": "failed", "exit_code": 10},
        )
    )
    assert text == "Operation failed: Data\nResult: failed\nExit code: 10"
