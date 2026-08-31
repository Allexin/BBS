"""Sanitize administrative JSONL into atomic daily static Web projections."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict

from backup_system.manager.journal import JournalRecord, Severity

_RETENTION_DAYS = 60


class _PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicLogRecord(_PublicModel):
    schema_version: Literal[1] = 1
    event_id: UUID
    timestamp: AwareDatetime
    severity: Severity
    component: str
    event: str
    job_display_name: str | None = None
    operation_id: UUID | None = None
    run_id: UUID | None = None
    operation_kind: str | None = None
    stage: str | None = None
    reason: str | None = None


class PublicLogDay(_PublicModel):
    schema_version: Literal[1] = 1
    generation_id: UUID
    date: date
    updated_at: AwareDatetime
    records: tuple[PublicLogRecord, ...]


class PublicLogIndexEntry(_PublicModel):
    date: date
    file: str
    generation_id: UUID
    sha256: str
    record_count: int
    updated_at: AwareDatetime


class PublicLogIndex(_PublicModel):
    schema_version: Literal[1] = 1
    generation_id: UUID
    generated_at: AwareDatetime
    days: tuple[PublicLogIndexEntry, ...]


class LogProjectionPublisher:
    def __init__(self, public_logs_directory: Path) -> None:
        self._directory = public_logs_directory

    def publish_day(
        self,
        source: Path,
        *,
        local_date: date,
        updated_at: datetime,
        job_display_names: dict[str, str],
    ) -> PublicLogDay:
        records: list[PublicLogRecord] = []
        if source.is_file():
            with source.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = JournalRecord.model_validate_json(line)
                    except ValueError:
                        continue
                    records.append(_sanitize(record, job_display_names))
        projection = PublicLogDay(
            generation_id=uuid4(),
            date=local_date,
            updated_at=updated_at,
            records=tuple(records),
        )
        self._directory.mkdir(parents=True, exist_ok=True)
        self._delete_expired(local_date)
        _replace_json(self._directory / f"{local_date.isoformat()}.json", projection)
        self.publish_index(generated_at=updated_at)
        return projection

    def publish_index(self, *, generated_at: datetime) -> PublicLogIndex:
        self._directory.mkdir(parents=True, exist_ok=True)
        return self._publish_index(generated_at=generated_at)

    def _delete_expired(self, local_date: date) -> None:
        cutoff = local_date - timedelta(days=_RETENTION_DAYS)
        for path in self._directory.glob("????-??-??.json"):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if file_date < cutoff and path.is_file() and not path.is_symlink():
                path.unlink()

    def _publish_index(self, *, generated_at: datetime) -> PublicLogIndex:
        days: list[PublicLogIndexEntry] = []
        for path in sorted(self._directory.glob("????-??-??.json"), reverse=True):
            try:
                day = PublicLogDay.model_validate_json(path.read_text(encoding="utf-8"))
                payload = path.read_bytes()
            except (OSError, ValueError):
                continue
            days.append(
                PublicLogIndexEntry(
                    date=day.date,
                    file=path.name,
                    generation_id=day.generation_id,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    record_count=len(day.records),
                    updated_at=day.updated_at,
                )
            )
        index = PublicLogIndex(
            generation_id=uuid4(),
            generated_at=generated_at,
            days=tuple(days),
        )
        _replace_json(self._directory / "index.json", index)
        return index


def _sanitize(record: JournalRecord, job_display_names: dict[str, str]) -> PublicLogRecord:
    details = record.details
    return PublicLogRecord(
        event_id=record.event_id,
        timestamp=record.timestamp,
        severity=record.severity,
        component=record.component,
        event=record.event,
        job_display_name=(
            job_display_names.get(record.job_id) if record.job_id is not None else None
        ),
        operation_id=record.operation_id,
        run_id=record.run_id,
        operation_kind=_optional_text(details.get("operation_kind")),
        stage=_optional_text(details.get("stage")),
        reason=_optional_text(details.get("public_reason")),
    )


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _replace_json(path: Path, value: _PublicModel) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    payload = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
