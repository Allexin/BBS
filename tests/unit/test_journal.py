import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backup_system.manager import journal
from backup_system.manager.journal import JournalWriter


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def test_journal_writes_unicode_and_embedded_newlines_as_one_json_line(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 20, 0, tzinfo=UTC))
    with JournalWriter(tmp_path, "Europe/Samara", clock=clock) as writer:
        record = writer.write(
            severity="info",
            component="manager",
            event="stage_changed",
            message="Кириллица\nsecond line",
        )

    lines = (tmp_path / "2026-08-29.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event_id"] == str(record.event_id)
    assert payload["timestamp"].endswith("Z")
    assert payload["message"] == "Кириллица\nsecond line"


def test_journal_rotates_on_first_write_after_local_midnight(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 19, 59, tzinfo=UTC))
    with JournalWriter(tmp_path, "Europe/Samara", clock=clock) as writer:
        writer.write(severity="info", component="manager", event="before_midnight")
        clock.value = datetime(2026, 8, 28, 20, 1, tzinfo=UTC)
        writer.write(severity="info", component="manager", event="after_midnight")

    assert (tmp_path / "2026-08-28.jsonl").is_file()
    assert (tmp_path / "2026-08-29.jsonl").is_file()


def test_retention_deletes_only_owned_daily_files(tmp_path: Path) -> None:
    (tmp_path / "2026-06-29.jsonl").write_text("old", encoding="utf-8")
    (tmp_path / "2026-06-30.jsonl").write_text("boundary", encoding="utf-8")
    unrelated = tmp_path / "bootstrap.jsonl"
    unrelated.write_text("keep", encoding="utf-8")

    clock = MutableClock(datetime(2026, 8, 28, 20, 0, tzinfo=UTC))
    with JournalWriter(tmp_path, "Europe/Samara", clock=clock):
        pass

    assert not (tmp_path / "2026-06-29.jsonl").exists()
    assert (tmp_path / "2026-06-30.jsonl").exists()
    assert unrelated.exists()


def test_warning_and_terminal_events_receive_durable_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushed: list[int] = []
    monkeypatch.setattr(journal, "_durable_flush", flushed.append)
    with JournalWriter(tmp_path, "UTC") as writer:
        writer.write(severity="info", component="manager", event="run_started")
        writer.write(severity="warning", component="manager", event="warning")
        writer.write(severity="info", component="manager", event="run_finished")
    assert len(flushed) == 2


def test_progress_is_not_persisted(tmp_path: Path) -> None:
    with JournalWriter(tmp_path, "UTC") as writer, pytest.raises(ValueError, match="progress"):
        writer.write(severity="info", component="manager", event="progress")
