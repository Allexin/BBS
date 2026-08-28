import json
from pathlib import Path
from uuid import UUID

import pytest

from backup_system.executor.vss import VssSnapshot
from backup_system.executor.vss_intent import (
    OwnedVssCleanupError,
    OwnedVssSnapshotManager,
    VssIntentError,
    VssIntentStore,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
SET_ID = UUID("22222222-2222-4222-8222-222222222222")
SNAPSHOT_ID = UUID("33333333-3333-4333-8333-333333333333")
VOLUME_ID = UUID("44444444-4444-4444-8444-444444444444")


class Cleaner:
    def __init__(self) -> None:
        self.deleted: list[UUID] = []

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
        self.deleted.append(snapshot_set_id)


def test_prepare_is_durable_before_created_identity_is_added(tmp_path: Path) -> None:
    store = VssIntentStore(tmp_path)
    prepared = store.prepare(
        job_id="job-1",
        run_id=RUN_ID,
        source_volume_guid=str(VOLUME_ID),
        snapshot_set_id=SET_ID,
    )
    assert store.load("job-1") == prepared
    assert prepared.state == "prepared"
    assert prepared.snapshot_id is None

    created = store.mark_created(prepared, SNAPSHOT_ID)
    assert store.load("job-1") == created
    assert created.state == "created"


def test_recover_deletes_only_exact_owned_set_then_clears_intent(tmp_path: Path) -> None:
    store = VssIntentStore(tmp_path)
    store.prepare(
        job_id="job-1",
        run_id=RUN_ID,
        source_volume_guid=str(VOLUME_ID),
        snapshot_set_id=SET_ID,
    )
    cleaner = Cleaner()
    assert store.recover("job-1", cleaner)
    assert cleaner.deleted == [SET_ID]
    assert store.load("job-1") is None
    assert not store.recover("job-1", cleaner)


def test_failed_cleanup_preserves_intent_for_future_recovery(tmp_path: Path) -> None:
    class FailingCleaner(Cleaner):
        def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
            raise RuntimeError("VSS unavailable")

    store = VssIntentStore(tmp_path)
    prepared = store.prepare(
        job_id="job-1",
        run_id=RUN_ID,
        source_volume_guid=str(VOLUME_ID),
        snapshot_set_id=SET_ID,
    )
    with pytest.raises(RuntimeError, match="VSS unavailable"):
        store.recover("job-1", FailingCleaner())
    assert store.load("job-1") == prepared


def test_tampered_intent_never_triggers_cleanup(tmp_path: Path) -> None:
    store = VssIntentStore(tmp_path)
    store.prepare(
        job_id="job-1",
        run_id=RUN_ID,
        source_volume_guid=str(VOLUME_ID),
        snapshot_set_id=SET_ID,
    )
    path = tmp_path / "job-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    cleaner = Cleaner()
    with pytest.raises(VssIntentError, match="cannot prove ownership"):
        store.recover("job-1", cleaner)
    assert cleaner.deleted == []


def test_second_unfinished_intent_is_rejected(tmp_path: Path) -> None:
    store = VssIntentStore(tmp_path)
    arguments = {
        "job_id": "job-1",
        "run_id": RUN_ID,
        "source_volume_guid": str(VOLUME_ID),
        "snapshot_set_id": SET_ID,
    }
    store.prepare(**arguments)
    with pytest.raises(VssIntentError, match="already exists"):
        store.prepare(**arguments)


class PreparedBackend(Cleaner):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def start_snapshot_set(self) -> UUID:
        self.calls.append("start")
        return SET_ID

    def complete_snapshot_set(self, snapshot_set_id: UUID, volume_guid: str) -> VssSnapshot:
        self.calls.append("complete")
        return VssSnapshot(SET_ID, SNAPSHOT_ID, volume_guid, "shadow-path")

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
        self.calls.append("delete")
        super().delete_snapshot_set(snapshot_set_id)


def test_owned_manager_persists_set_before_snapshot_creation(tmp_path: Path) -> None:
    backend = PreparedBackend()
    store = VssIntentStore(tmp_path)

    def action(snapshot: VssSnapshot) -> str:
        backend.calls.append("action")
        assert store.load("job-1").snapshot_id == SNAPSHOT_ID  # type: ignore[union-attr]
        return "done"

    result = OwnedVssSnapshotManager(backend, store).run(
        job_id="job-1", run_id=RUN_ID, volume_guid=str(VOLUME_ID), action=action
    )
    assert result == "done"
    assert backend.calls == ["start", "complete", "action", "delete"]
    assert store.load("job-1") is None


def test_snapshot_creation_failure_deletes_exact_prepared_set(tmp_path: Path) -> None:
    class FailingBackend(PreparedBackend):
        def complete_snapshot_set(self, snapshot_set_id: UUID, volume_guid: str) -> VssSnapshot:
            raise RuntimeError("creation failed")

    backend = FailingBackend()
    store = VssIntentStore(tmp_path)
    with pytest.raises(RuntimeError, match="creation failed"):
        OwnedVssSnapshotManager(backend, store).run(
            job_id="job-1",
            run_id=RUN_ID,
            volume_guid=str(VOLUME_ID),
            action=lambda snapshot: None,
        )
    assert backend.deleted == [SET_ID]
    assert store.load("job-1") is None


def test_cleanup_failure_preserves_intent_and_primary_error(tmp_path: Path) -> None:
    class CleanupFailure(PreparedBackend):
        def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
            raise RuntimeError("delete failed")

    store = VssIntentStore(tmp_path)
    with pytest.raises(OwnedVssCleanupError) as raised:
        OwnedVssSnapshotManager(CleanupFailure(), store).run(
            job_id="job-1",
            run_id=RUN_ID,
            volume_guid=str(VOLUME_ID),
            action=lambda snapshot: (_ for _ in ()).throw(ValueError("action failed")),
        )
    assert isinstance(raised.value.primary_error, ValueError)
    assert store.load("job-1") is not None
