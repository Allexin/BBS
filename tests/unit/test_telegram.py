from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from backup_system.manager.database import open_manager_database
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.telegram import (
    DispatchResult,
    NotificationDispatcher,
    TelegramSender,
)


def _sender(handler: httpx.MockTransport) -> TelegramSender:
    return TelegramSender(
        token="test-token",
        chat_id="test-chat",
        client=httpx.Client(transport=handler),
    )


def test_sender_posts_expected_telegram_request() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bottest-token/sendMessage"
        assert request.read() == b'{"chat_id":"test-chat","text":"hello"}'
        return httpx.Response(200, json={"ok": True})

    _sender(httpx.MockTransport(handle)).send("hello")


def test_sender_can_target_forum_topic() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.read() == (
            b'{"chat_id":"test-chat","text":"hello","message_thread_id":42}'
        )
        return httpx.Response(200, json={"ok": True})

    TelegramSender(
        token="test-token",
        chat_id="test-chat",
        message_thread_id=42,
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    ).send("hello")


def test_dispatcher_marks_successful_notification_sent(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    repository = NotificationRepository(connection)
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        repository.enqueue(
            deduplication_key="alert:one",
            kind="alert",
            payload={"text": "attention"},
            created_at=now,
        )
        dispatcher = NotificationDispatcher(
            repository,
            _sender(httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
            lambda item: str(item.payload["text"]),
        )
        assert dispatcher.dispatch_one(now=now) is DispatchResult.SENT
        assert dispatcher.dispatch_one(now=now) is DispatchResult.IDLE
    finally:
        connection.close()


def test_transport_failure_schedules_retry_without_escaping(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    repository = NotificationRepository(connection)
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    try:
        notification_id, _ = repository.enqueue(
            deduplication_key="alert:failure",
            kind="alert",
            payload={"text": "attention"},
            created_at=now,
        )

        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("contains-secret-url", request=request)

        dispatcher = NotificationDispatcher(
            repository,
            _sender(httpx.MockTransport(fail)),
            lambda item: str(item.payload["text"]),
        )
        assert dispatcher.dispatch_one(now=now) is DispatchResult.RETRY_SCHEDULED
        row = connection.execute(
            """SELECT state, attempts, last_error, next_attempt_at
            FROM notifications WHERE notification_id = ?""",
            (str(notification_id),),
        ).fetchone()
        assert row == (
            "pending",
            1,
            "telegram transport failed",
            (now + timedelta(seconds=30)).isoformat(),
        )
    finally:
        connection.close()
