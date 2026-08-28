from backup_system.common.events import Progress
from backup_system.executor.snapshot_runtime import _emit_progress


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
