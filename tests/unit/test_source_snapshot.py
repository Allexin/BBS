from pathlib import Path, PureWindowsPath
from uuid import UUID

import pytest

from backup_system.executor.cancellation import CancellationRequested, CancellationToken
from backup_system.executor.source_snapshot import ExecutorSourceSnapshot, SourceSnapshotError
from backup_system.executor.source_volume import ResolvedSourceVolume
from backup_system.executor.vss import VssSnapshot
from backup_system.executor.vss_intent import OwnedVssSnapshotManager, VssIntentStore

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
SET_ID = UUID("22222222-2222-4222-8222-222222222222")
SNAPSHOT_ID = UUID("33333333-3333-4333-8333-333333333333")
VOLUME_ID = UUID("44444444-4444-4444-8444-444444444444")


class _Resolver:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    def resolve(self, source_path: str) -> ResolvedSourceVolume:
        self._calls.append(("resolve", source_path))
        return ResolvedSourceVolume(
            VOLUME_ID,
            f"\\\\?\\Volume{{{VOLUME_ID}}}\\",
            PureWindowsPath(r"Data\Current"),
        )


class _Backend:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    def start_snapshot_set(self) -> UUID:
        self._calls.append("start")
        return SET_ID

    def complete_snapshot_set(self, snapshot_set_id: UUID, volume_guid: str) -> VssSnapshot:
        self._calls.append(("complete", snapshot_set_id, volume_guid))
        return VssSnapshot(
            SET_ID,
            SNAPSHOT_ID,
            volume_guid,
            "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy9\\",
        )

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
        self._calls.append(("delete", snapshot_set_id))


def _runtime(
    tmp_path: Path,
    calls: list[object],
    token: CancellationToken,
    *,
    directory_exists: bool = True,
) -> tuple[ExecutorSourceSnapshot, VssIntentStore]:
    store = VssIntentStore(tmp_path)
    runtime = ExecutorSourceSnapshot(
        resolver=_Resolver(calls),
        snapshots=OwnedVssSnapshotManager(_Backend(calls), store),
        cancellation_checkpoint=token.raise_if_requested,
        directory_check=lambda path: directory_exists,
    )
    return runtime, store


def test_adapter_receives_only_relative_root_inside_shadow(tmp_path: Path) -> None:
    calls: list[object] = []
    token = CancellationToken()
    runtime, store = _runtime(tmp_path, calls, token)

    result = runtime.run(
        job_id="job-1",
        run_id=RUN_ID,
        source_path=r"F:\Data\Current",
        action=lambda path: calls.append(("action", path)) or "done",
    )

    assert result == "done"
    assert calls == [
        ("resolve", r"F:\Data\Current"),
        "start",
        ("complete", SET_ID, str(VOLUME_ID)),
        (
            "action",
            PureWindowsPath(r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy9\Data\Current"),
        ),
        ("delete", SET_ID),
    ]
    assert store.load("job-1") is None


def test_unreadable_shadow_root_fails_before_adapter_and_cleans_snapshot(
    tmp_path: Path,
) -> None:
    calls: list[object] = []
    runtime, store = _runtime(
        tmp_path,
        calls,
        CancellationToken(),
        directory_exists=False,
    )

    with pytest.raises(SourceSnapshotError, match="not readable"):
        runtime.run(
            job_id="job-1",
            run_id=RUN_ID,
            source_path=r"F:\Data\Current",
            action=lambda path: calls.append("action"),
        )

    assert "action" not in calls
    assert calls[-1] == ("delete", SET_ID)
    assert store.load("job-1") is None


def test_cancellation_before_adapter_still_cleans_snapshot(tmp_path: Path) -> None:
    calls: list[object] = []
    token = CancellationToken()

    def directory_check(path: PureWindowsPath) -> bool:
        token.request()
        return True

    store = VssIntentStore(tmp_path)
    runtime = ExecutorSourceSnapshot(
        resolver=_Resolver(calls),
        snapshots=OwnedVssSnapshotManager(_Backend(calls), store),
        cancellation_checkpoint=token.raise_if_requested,
        directory_check=directory_check,
    )

    with pytest.raises(CancellationRequested):
        runtime.run(
            job_id="job-1",
            run_id=RUN_ID,
            source_path=r"F:\Data\Current",
            action=lambda path: calls.append("action"),
        )

    assert "action" not in calls
    assert calls[-1] == ("delete", SET_ID)
    assert store.load("job-1") is None
