"""Telegram transport and one-at-a-time durable outbox delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import httpx

from backup_system.manager.database import open_manager_database
from backup_system.manager.notification_format import render_notification
from backup_system.manager.notifications import NotificationRepository, PendingNotification


class TelegramDeliveryError(RuntimeError):
    """A sanitized delivery failure that never includes credentials or response bodies."""


class DispatchResult(StrEnum):
    IDLE = "idle"
    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"


class TelegramSender:
    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        client: httpx.Client,
        message_thread_id: int | None = None,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram token and chat ID must not be empty")
        self._token = token
        self._chat_id = chat_id
        self._client = client
        if message_thread_id is not None and message_thread_id <= 0:
            raise ValueError("Telegram message thread ID must be positive")
        self._message_thread_id = message_thread_id

    def send(self, text: str) -> None:
        if not text:
            raise ValueError("Telegram message must not be empty")
        try:
            payload: dict[str, str | int] = {"chat_id": self._chat_id, "text": text}
            if self._message_thread_id is not None:
                payload["message_thread_id"] = self._message_thread_id
            response = self._client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json=payload,
            )
        except httpx.HTTPError as error:
            raise TelegramDeliveryError("telegram transport failed") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise TelegramDeliveryError(f"telegram returned HTTP status {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise TelegramDeliveryError("telegram returned invalid JSON") from error
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramDeliveryError("telegram rejected the message")


class NotificationDispatcher:
    def __init__(
        self,
        repository: NotificationRepository,
        sender: TelegramSender,
        renderer: Callable[[PendingNotification], str],
    ) -> None:
        self._repository = repository
        self._sender = sender
        self._renderer = renderer

    def dispatch_one(self, *, now: datetime) -> DispatchResult:
        notification = self._repository.next_due(now=now)
        if notification is None:
            return DispatchResult.IDLE
        try:
            self._sender.send(self._renderer(notification))
        except TelegramDeliveryError as error:
            self._repository.record_failure(notification.notification_id, str(error), failed_at=now)
            return DispatchResult.RETRY_SCHEDULED
        self._repository.record_sent(notification.notification_id, sent_at=now)
        return DispatchResult.SENT


class AsyncNotificationDispatcher:
    """Dispatch from a worker thread with its own SQLite connection."""

    def __init__(
        self,
        database: Path,
        *,
        token: str,
        chat_id: str,
        message_thread_id: int | None,
    ) -> None:
        self._database = database
        self._token = token
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id

    async def dispatch_one(self, *, now: datetime) -> DispatchResult:
        return await asyncio.to_thread(
            _dispatch_pending,
            self._database,
            self._token,
            self._chat_id,
            self._message_thread_id,
            now,
        )


def _dispatch_pending(
    database: Path,
    token: str,
    chat_id: str,
    message_thread_id: int | None,
    now: datetime,
) -> DispatchResult:
    connection = open_manager_database(database)
    try:
        with httpx.Client(timeout=10) as client:
            sender = TelegramSender(
                token=token,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                client=client,
            )
            return NotificationDispatcher(
                NotificationRepository(connection), sender, render_notification
            ).dispatch_one(now=now)
    finally:
        connection.close()
