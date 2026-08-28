"""Daily UTF-8 JSONL administrative journal with calendar retention."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backup_system.common.ids import new_event_id, parse_uuid4
from backup_system.common.time import require_aware, utc_now

Severity = Literal["debug", "info", "warning", "error", "critical"]
RETENTION_DAYS = 60
_DURABLE_SEVERITIES = {"warning", "error", "critical"}
_TERMINAL_EVENTS = {"run_finished"}


class JournalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_id: UUID
    timestamp: datetime
    severity: Severity
    component: str = Field(min_length=1)
    event: str = Field(min_length=1)
    message: str | None = None
    operation_id: UUID | None = None
    run_id: UUID | None = None
    job_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id", "operation_id", "run_id")
    @classmethod
    def uuid4_only(cls, value: UUID | None) -> UUID | None:
        return parse_uuid4(str(value)) if value is not None else None

    @field_validator("timestamp")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        value = require_aware(value)
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("journal timestamp must be UTC")
        return value


class JournalWriter:
    def __init__(
        self,
        logs_directory: Path,
        timezone: str,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._logs_directory = logs_directory
        self._timezone = ZoneInfo(timezone)
        self._clock = clock
        self._lock = threading.Lock()
        self._stream: IO[str] | None = None
        self._open_date: date | None = None
        self._rotate_if_needed(self._local_date())

    def write(
        self,
        *,
        severity: Severity,
        component: str,
        event: str,
        message: str | None = None,
        operation_id: UUID | None = None,
        run_id: UUID | None = None,
        job_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> JournalRecord:
        if event == "progress":
            raise ValueError("progress events are not persisted in the journal")
        record = JournalRecord(
            event_id=new_event_id(),
            timestamp=require_aware(timestamp or self._clock()).astimezone(UTC),
            severity=severity,
            component=component,
            event=event,
            message=message,
            operation_id=operation_id,
            run_id=run_id,
            job_id=job_id,
            details=dict(details or {}),
        )
        line = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            self._rotate_if_needed(self._local_date())
            if self._stream is None:
                raise RuntimeError("journal is closed")
            self._stream.write(line + "\n")
            self._stream.flush()
            if severity in _DURABLE_SEVERITIES or event in _TERMINAL_EVENTS:
                _durable_flush(self._stream.fileno())
        return record

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.flush()
                self._stream.close()
                self._stream = None

    def __enter__(self) -> JournalWriter:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _local_date(self) -> date:
        return require_aware(self._clock()).astimezone(self._timezone).date()

    def _rotate_if_needed(self, local_date: date) -> None:
        if self._open_date == local_date and self._stream is not None:
            return
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()
        self._logs_directory.mkdir(parents=True, exist_ok=True)
        path = self._logs_directory / f"{local_date.isoformat()}.jsonl"
        self._stream = path.open("a", encoding="utf-8", newline="")
        self._open_date = local_date
        self._delete_expired(local_date)

    def _delete_expired(self, local_date: date) -> None:
        cutoff = local_date - timedelta(days=RETENTION_DAYS)
        for path in self._logs_directory.glob("????-??-??.jsonl"):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if file_date < cutoff and path.is_file() and not path.is_symlink():
                path.unlink()


def _durable_flush(file_descriptor: int) -> None:
    """Map to the platform durable file flush (`FlushFileBuffers` on Windows)."""
    os.fsync(file_descriptor)
