"""Timezone-safe clock helpers."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return value
