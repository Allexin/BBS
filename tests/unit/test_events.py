from datetime import UTC, datetime

from backup_system.common.events import (
    DiskOfflineConfirmed,
    Progress,
    UnknownExecutorEvent,
    parse_executor_event,
)


def test_known_event_is_strictly_parsed() -> None:
    event = parse_executor_event(
        {
            "schema_version": 1,
            "event": "progress",
            "timestamp": datetime.now(UTC).isoformat(),
            "stage": "backing_up",
            "bytes_done": 10,
            "bytes_total": 20,
        }
    )
    assert isinstance(event, Progress)


def test_unknown_event_is_forward_compatible() -> None:
    event = parse_executor_event(
        {
            "schema_version": 1,
            "event": "future_event",
            "timestamp": datetime.now(UTC).isoformat(),
            "future_field": 42,
        }
    )
    assert isinstance(event, UnknownExecutorEvent)


def test_disk_offline_confirmation_is_a_known_strict_event() -> None:
    event = parse_executor_event(
        {
            "schema_version": 1,
            "event": "disk_offline_confirmed",
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    assert isinstance(event, DiskOfflineConfirmed)
