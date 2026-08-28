from uuid import UUID

import pytest

from backup_system.executor.native_vss import (
    HRESULT_OPERATION_CANCELLED,
    HRESULT_WAIT_TIMEOUT,
    VSS_S_ASYNC_CANCELLED,
    VSS_S_ASYNC_FINISHED,
    VSS_S_ASYNC_PENDING,
    NativeVssBackend,
    _require_async_finished,
)
from backup_system.executor.vss import VssSnapshot
from backup_system.executor.windows_vss import VssBackendError

SET_ID = UUID("11111111-1111-4111-8111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-4222-8222-222222222222")
VOLUME_ID = UUID("33333333-3333-4333-8333-333333333333")


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def start_snapshot_set(self) -> UUID:
        self.calls.append("start")
        return SET_ID

    def complete_snapshot_set(self, snapshot_set_id: UUID, volume_name: str) -> VssSnapshot:
        self.calls.append(("complete", snapshot_set_id, volume_name))
        return VssSnapshot(SET_ID, SNAPSHOT_ID, volume_name, "shadow")

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
        self.calls.append(("delete", snapshot_set_id))

    def close(self) -> None:
        self.calls.append("close")


class FakeFactory:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    def create(self) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session


def test_active_requestor_is_retained_across_start_complete_and_delete() -> None:
    factory = FakeFactory()
    backend = NativeVssBackend(factory)
    assert backend.start_snapshot_set() == SET_ID
    snapshot = backend.complete_snapshot_set(SET_ID, str(VOLUME_ID))
    backend.delete_snapshot_set(SET_ID)
    assert snapshot.snapshot_id == SNAPSHOT_ID
    assert factory.sessions[0].calls == [
        "start",
        ("complete", SET_ID, f"\\\\?\\Volume{{{VOLUME_ID}}}\\"),
        ("delete", SET_ID),
        "close",
    ]


def test_recovery_without_active_requestor_uses_fresh_session() -> None:
    factory = FakeFactory()
    NativeVssBackend(factory).delete_snapshot_set(SET_ID)
    assert factory.sessions[0].calls == [("delete", SET_ID), "close"]


def test_different_set_cannot_use_active_requestor() -> None:
    backend = NativeVssBackend(FakeFactory())
    backend.start_snapshot_set()
    with pytest.raises(VssBackendError, match="not owned"):
        backend.complete_snapshot_set(UUID(int=4), str(VOLUME_ID))


def test_second_active_set_is_rejected() -> None:
    backend = NativeVssBackend(FakeFactory())
    backend.start_snapshot_set()
    with pytest.raises(VssBackendError, match="another set"):
        backend.start_snapshot_set()


def test_async_status_requires_finished_and_classifies_bounded_wait_failures() -> None:
    _require_async_finished(VSS_S_ASYNC_FINISHED, "DoSnapshotSet")

    with pytest.raises(VssBackendError) as pending:
        _require_async_finished(VSS_S_ASYNC_PENDING, "DoSnapshotSet")
    assert pending.value.code == HRESULT_WAIT_TIMEOUT

    with pytest.raises(VssBackendError) as cancelled:
        _require_async_finished(VSS_S_ASYNC_CANCELLED, "DoSnapshotSet")
    assert cancelled.value.code == HRESULT_OPERATION_CANCELLED
