from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from backup_system.manager.database import open_manager_database
from backup_system.manager.notifications import NotificationRepository, NotificationState


def test_notification_is_deduplicated_and_sent(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    repository = NotificationRepository(connection)
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        first_id, created = repository.enqueue(
            deduplication_key="run:failed:one",
            kind="run_failed",
            payload={"job": "Data"},
            created_at=now,
        )
        duplicate_id, duplicate_created = repository.enqueue(
            deduplication_key="run:failed:one",
            kind="run_failed",
            payload={"job": "Changed"},
            created_at=now,
        )
        assert created and not duplicate_created and duplicate_id == first_id
        pending = repository.next_due(now=now)
        assert pending is not None and pending.payload == {"job": "Data"}

        repository.record_sent(first_id, sent_at=now)
        assert repository.next_due(now=now) is None
        assert connection.execute("SELECT state, sent_at FROM notifications").fetchone() == (
            NotificationState.SENT,
            now.isoformat(),
        )
    finally:
        connection.close()


def test_delivery_failure_is_retried_without_losing_notification(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    repository = NotificationRepository(connection)
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        notification_id, _ = repository.enqueue(
            deduplication_key="startup:one",
            kind="startup_report",
            payload={},
            created_at=now,
        )
        retry_at = repository.record_failure(notification_id, "network down", failed_at=now)
        assert retry_at == now + timedelta(seconds=30)
        assert repository.next_due(now=retry_at - timedelta(microseconds=1)) is None
        assert repository.next_due(now=retry_at) is not None
    finally:
        connection.close()


def test_unknown_or_terminal_notification_cannot_transition(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    repository = NotificationRepository(connection)
    try:
        with pytest.raises(RuntimeError, match="pending"):
            repository.record_sent(uuid4())
    finally:
        connection.close()
