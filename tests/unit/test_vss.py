from uuid import UUID, uuid4

import pytest

from backup_system.executor.vss import VssCleanupError, VssSnapshot, VssSnapshotManager


class FakeVss:
    def __init__(self) -> None:
        self.snapshot = VssSnapshot(uuid4(), uuid4(), "volume", "\\\\?\\shadow\\")
        self.deleted: list[UUID] = []
        self.delete_error: BaseException | None = None

    def create_client_accessible_snapshot(self, volume_guid: str) -> VssSnapshot:
        return self.snapshot

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
        self.deleted.append(snapshot_set_id)
        if self.delete_error:
            raise self.delete_error


def test_vss_deletes_exact_owned_set_after_success_and_failure() -> None:
    backend = FakeVss()
    assert VssSnapshotManager(backend).run("volume", lambda snapshot: "done") == "done"
    assert backend.deleted == [backend.snapshot.snapshot_set_id]

    backend.deleted.clear()
    with pytest.raises(ValueError, match="action failed"):
        VssSnapshotManager(backend).run(
            "volume", lambda snapshot: (_ for _ in ()).throw(ValueError("action failed"))
        )
    assert backend.deleted == [backend.snapshot.snapshot_set_id]


def test_vss_cleanup_failure_preserves_primary_error() -> None:
    backend = FakeVss()
    backend.delete_error = OSError("delete failed")
    primary = ValueError("data failed")
    with pytest.raises(VssCleanupError) as captured:
        VssSnapshotManager(backend).run("volume", lambda snapshot: (_ for _ in ()).throw(primary))
    assert captured.value.primary_error is primary
    assert captured.value.snapshot == backend.snapshot
