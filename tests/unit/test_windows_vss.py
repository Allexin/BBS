from uuid import UUID

import pytest

from backup_system.executor.windows_vss import (
    CLIENT_ACCESSIBLE,
    ShadowRecord,
    VssBackendError,
    WindowsVssBackend,
)

SNAPSHOT_ID = UUID("11111111-1111-1111-1111-111111111111")
SET_ID = UUID("22222222-2222-2222-2222-222222222222")
VOLUME_ID = UUID("33333333-3333-3333-3333-333333333333")


class FakeSource:
    def __init__(self) -> None:
        self.record = ShadowRecord(
            SNAPSHOT_ID,
            SET_ID,
            f"\\\\?\\Volume{{{VOLUME_ID}}}\\",
            r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy7",
        )
        self.created: list[tuple[str, str]] = []
        self.deleted: list[UUID] = []

    def create(self, volume_name: str, context: str) -> UUID:
        self.created.append((volume_name, context))
        return SNAPSHOT_ID

    def get(self, snapshot_id: UUID) -> ShadowRecord | None:
        return self.record if snapshot_id == SNAPSHOT_ID else None

    def in_set(self, snapshot_set_id: UUID) -> tuple[ShadowRecord, ...]:
        if snapshot_set_id == SET_ID and SNAPSHOT_ID not in self.deleted:
            return (self.record,)
        return ()

    def delete(self, snapshot_id: UUID) -> None:
        self.deleted.append(snapshot_id)


def test_create_validates_volume_and_returns_owned_set() -> None:
    source = FakeSource()
    snapshot = WindowsVssBackend(source).create_client_accessible_snapshot(str(VOLUME_ID))
    assert source.created == [(f"\\\\?\\Volume{{{VOLUME_ID}}}\\", CLIENT_ACCESSIBLE)]
    assert snapshot.snapshot_id == SNAPSHOT_ID
    assert snapshot.snapshot_set_id == SET_ID
    assert snapshot.shadow_device_path.endswith("\\")


def test_delete_removes_only_snapshots_in_exact_set_and_verifies_absence() -> None:
    source = FakeSource()
    WindowsVssBackend(source).delete_snapshot_set(SET_ID)
    assert source.deleted == [SNAPSHOT_ID]


def test_created_snapshot_on_unexpected_volume_is_rejected() -> None:
    source = FakeSource()
    source.record = ShadowRecord(
        SNAPSHOT_ID,
        SET_ID,
        "\\\\?\\Volume{44444444-4444-4444-4444-444444444444}\\",
        source.record.device_path,
    )
    with pytest.raises(VssBackendError, match="verify source volume"):
        WindowsVssBackend(source).create_client_accessible_snapshot(str(VOLUME_ID))
    assert source.deleted == [SNAPSHOT_ID]


def test_delete_failure_keeps_normalized_numeric_code() -> None:
    class FailingSource(FakeSource):
        def delete(self, snapshot_id: UUID) -> None:
            raise VssBackendError("delete snapshot", 12)

    with pytest.raises(VssBackendError) as raised:
        WindowsVssBackend(FailingSource()).delete_snapshot_set(SET_ID)
    assert raised.value.operation == "delete snapshot"
    assert raised.value.code == 12
