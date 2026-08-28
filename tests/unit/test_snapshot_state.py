from datetime import UTC, datetime
from pathlib import Path

from backup_system.executor.snapshot_state import SnapshotStateStore


def _store(tmp_path: Path) -> SnapshotStateStore:
    return SnapshotStateStore(tmp_path / "state", tmp_path / "diagnostics")


def test_subset_cursor_advances_only_when_committed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    loaded = store.load("data", subset_parts=4)
    assert loaded.state.next_subset_part == 1

    unchanged = store.load("data", subset_parts=4)
    assert unchanged.state.next_subset_part == 1

    advanced = store.complete_check("data", unchanged.state, mode="subset")
    assert advanced.next_subset_part == 2


def test_fourth_subset_records_full_cycle_and_wraps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.load("data", subset_parts=4).state
    for _ in range(3):
        state = store.complete_check("data", state, mode="subset")
    completed = store.complete_check(
        "data", state, mode="subset", now=datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert completed.next_subset_part == 1
    assert completed.cycle_started_at is None
    assert completed.last_full_cycle_at == "2026-01-02T00:00:00+00:00"


def test_corrupt_cursor_is_archived_and_reset(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.load("data", subset_parts=4)
    (tmp_path / "state" / "data.json").write_text("broken", encoding="utf-8")

    loaded = store.load("data", subset_parts=4)

    assert loaded.cursor_reset is True
    assert loaded.archived_path is not None and loaded.archived_path.exists()
    assert loaded.state.next_subset_part == 1


def test_only_full_check_clears_verification_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.activate_gate("data", subset_parts=4)
    subset = store.complete_check("data", state, mode="subset")
    assert subset.verification_gate is True
    full = store.complete_check("data", subset, mode="full")
    assert full.verification_gate is False
