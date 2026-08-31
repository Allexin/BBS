import json
from datetime import UTC, date, datetime
from pathlib import Path

from backup_system.manager.journal import JournalWriter
from backup_system.manager.log_projection import LogProjectionPublisher, PublicLogIndex


def test_empty_index_is_published_before_first_journal_event(tmp_path: Path) -> None:
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    index = LogProjectionPublisher(tmp_path).publish_index(generated_at=now)

    assert index.days == ()
    stored = PublicLogIndex.model_validate_json(
        (tmp_path / "index.json").read_text(encoding="utf-8")
    )
    assert stored.days == ()


def test_log_projection_exposes_only_allowlisted_sanitized_fields(tmp_path: Path) -> None:
    source = tmp_path / "private"
    public = tmp_path / "public"
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    with JournalWriter(source, "UTC", clock=lambda: now) as writer:
        record = writer.write(
            severity="error",
            component="executor",
            event="run_failed",
            message=r"C:\Users\secret\file token=VERY_SECRET serial=DISK_SERIAL",
            job_id="internal-job-id",
            details={
                "operation_kind": "backup",
                "stage": "copying",
                "public_reason": "Read failed",
                "stderr": "VERY_SECRET",
                "repository_path": r"D:\private",
            },
            timestamp=now,
        )
    (source / "2026-08-28.jsonl").write_text(
        (source / "2026-08-28.jsonl").read_text(encoding="utf-8") + "not-json\n",
        encoding="utf-8",
    )

    day = LogProjectionPublisher(public).publish_day(
        source / "2026-08-28.jsonl",
        local_date=date(2026, 8, 28),
        updated_at=now,
        job_display_names={"internal-job-id": "Data"},
    )

    assert len(day.records) == 1
    assert day.records[0].event_id == record.event_id
    assert day.records[0].job_display_name == "Data"
    assert day.records[0].reason == "Read failed"
    serialized = (public / "2026-08-28.json").read_text(encoding="utf-8")
    for forbidden in ("VERY_SECRET", "DISK_SERIAL", "internal-job-id", "repository_path"):
        assert forbidden not in serialized

    index = PublicLogIndex.model_validate_json((public / "index.json").read_text(encoding="utf-8"))
    assert index.days[0].record_count == 1
    assert index.days[0].file == "2026-08-28.json"
    assert len(index.days[0].sha256) == 64


def test_republishing_day_replaces_array_instead_of_appending(tmp_path: Path) -> None:
    source = tmp_path / "private"
    public = tmp_path / "public"
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    publisher = LogProjectionPublisher(public)
    with JournalWriter(source, "UTC", clock=lambda: now) as writer:
        writer.write(severity="info", component="manager", event="first", timestamp=now)
    first = publisher.publish_day(
        source / "2026-08-28.jsonl",
        local_date=date(2026, 8, 28),
        updated_at=now,
        job_display_names={},
    )
    with JournalWriter(source, "UTC", clock=lambda: now) as writer:
        writer.write(severity="info", component="manager", event="second", timestamp=now)
    second = publisher.publish_day(
        source / "2026-08-28.jsonl",
        local_date=date(2026, 8, 28),
        updated_at=now,
        job_display_names={},
    )
    payload = json.loads((public / "2026-08-28.json").read_text(encoding="utf-8"))
    assert first.generation_id != second.generation_id
    assert [item["event"] for item in payload["records"]] == ["first", "second"]


def test_public_log_retention_removes_only_expired_owned_days(tmp_path: Path) -> None:
    source = tmp_path / "private"
    public = tmp_path / "public"
    source.mkdir()
    public.mkdir()
    (public / "2026-06-28.json").write_text("{}", encoding="utf-8")
    boundary = public / "2026-06-29.json"
    boundary.write_text("{}", encoding="utf-8")
    unrelated = public / "keep.json"
    unrelated.write_text("keep", encoding="utf-8")
    LogProjectionPublisher(public).publish_day(
        source / "2026-08-28.jsonl",
        local_date=date(2026, 8, 28),
        updated_at=datetime(2026, 8, 28, tzinfo=UTC),
        job_display_names={},
    )
    assert not (public / "2026-06-28.json").exists()
    assert boundary.exists()
    assert unrelated.exists()
