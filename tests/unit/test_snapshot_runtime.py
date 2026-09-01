from pathlib import Path
from uuid import uuid4

from backup_system.common.events import Progress, SourceReadWarning
from backup_system.executor.snapshot_runtime import _emit_progress, _emit_source_warning


def test_restic_status_is_translated_to_public_progress() -> None:
    events: list[object] = []
    _emit_progress(
        {
            "message_type": "status",
            "files_done": 2,
            "total_files": 3,
            "bytes_done": 5,
            "total_bytes": 8,
            "secret_path": r"C:\private",
        },
        events.append,
    )
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, Progress)
    assert event.files_done == 2
    assert "private" not in event.model_dump_json()


def test_non_status_restic_event_is_not_publicly_forwarded() -> None:
    events: list[object] = []
    _emit_progress({"message_type": "error", "error": "sensitive"}, events.append)
    assert events == []


def test_source_warning_writes_full_text_and_emits_bounded_preview(tmp_path: Path) -> None:
    events: list[object] = []
    run_id = uuid4()
    paths = tuple(f"T:\\bad-{index}" for index in range(12))

    _emit_source_warning(tmp_path, run_id, 12, paths, events.append)

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, SourceReadWarning)
    assert event.error_count == 12
    assert event.paths == paths[:10]
    report = tmp_path / "data" / "public" / "source-errors" / f"{run_id}.txt"
    assert report.read_text(encoding="utf-8").splitlines()[-12:] == list(paths)
