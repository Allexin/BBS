"""Telegram transport and one-at-a-time durable outbox delivery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

import httpx

from backup_system.manager.notifications import NotificationRepository, PendingNotification


class TelegramDeliveryError(RuntimeError):
    """A sanitized delivery failure that never includes credentials or response bodies."""


class DispatchResult(StrEnum):
    IDLE = "idle"
    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"


class TelegramSender:
    def __init__(self, *, token: str, chat_id: str, client: httpx.Client) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram token and chat ID must not be empty")
        self._token = token
        self._chat_id = chat_id
        self._client = client

    def send(self, text: str) -> None:
        if not text:
            raise ValueError("Telegram message must not be empty")
        try:
            response = self._client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text},
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
