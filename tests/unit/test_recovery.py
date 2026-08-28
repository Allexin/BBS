from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

from backup_system.common.config import DiskConfig, SmartConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.coordinator import ExecutorWindowsCoordinator
from backup_system.executor.disk_control import DiskObservation, VerifiedDisk, VolumeObservation
from backup_system.executor.lifecycle import ExecutorDiskLifecycle, LifecycleOperationError
from backup_system.executor.recovery import ExecutorRecovery
from backup_system.executor.smart_preflight import SmartPreflightObservation
from backup_system.executor.vss_intent import VssIntentStore

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
SET_ID = UUID("22222222-2222-4222-8222-222222222222")
VOLUME_ID = UUID("33333333-3333-4333-8333-333333333333")


def _disk() -> DiskConfig:
    return DiskConfig(
        physical_serial="serial",
        expected_size_bytes=1000,
        partition_guid="partition",
        volume_guid=str(VOLUME_ID),
        mount_point=r"C:\BackupVolumes\primary",
        repository_path_timeout_seconds=30,
    )


class _Control:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    def inspect(self, config: DiskConfig) -> DiskObservation:
        self._calls.append("inspect")
        return DiskObservation(VerifiedDisk("serial", 1000, "partition", str(VOLUME_ID)), False)

    def bring_online(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        raise AssertionError("recover must not bring the disk online")

    def ensure_repository_path(
        self, config: DiskConfig, verified_disk: VerifiedDisk
    ) -> VolumeObservation:
        raise AssertionError("recover must not mount the repository")

    def take_offline(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        self._calls.append("offline")


class _UnusedSmart:
    def collect(self, config: SmartConfig) -> tuple[SmartPreflightObservation, ...]:
        raise AssertionError("recover must not collect SMART")


class _Cleaner:
    def __init__(self, calls: list[object], *, fail: bool = False) -> None:
        self._calls = calls
        self._fail = fail

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
        self._calls.append(("delete", snapshot_set_id))
        if self._fail:
            raise RuntimeError("VSS cleanup failed")


def _coordinator(calls: list[object]) -> ExecutorWindowsCoordinator:
    @contextmanager
    def lock() -> Iterator[object]:
        calls.append("lock")
        try:
            yield object()
        finally:
            calls.append("unlock")

    return ExecutorWindowsCoordinator(
        lock_factory=lock,
        disk_lifecycle=ExecutorDiskLifecycle(_Control(calls)),
        smart=_UnusedSmart(),
        smart_sink=lambda observations: None,
        cancellation=CancellationToken(),
    )


def _prepare_intent(path: Path) -> VssIntentStore:
    store = VssIntentStore(path)
    store.prepare(
        job_id="job-1",
        run_id=RUN_ID,
        source_volume_guid=str(VOLUME_ID),
        snapshot_set_id=SET_ID,
    )
    return store


def test_recover_deletes_exact_owned_set_before_confirming_offline(tmp_path: Path) -> None:
    calls: list[object] = []
    store = _prepare_intent(tmp_path)
    result = ExecutorRecovery(
        coordinator=_coordinator(calls),
        intents=store,
        vss_cleaner=_Cleaner(calls),
    ).run(job_id="job-1", disk=_disk())

    assert calls == ["lock", "inspect", ("delete", SET_ID), "offline", "unlock"]
    assert result.vss_intent_cleaned is True
    assert result.disk_offline_confirmed is True
    assert store.load("job-1") is None


def test_recover_without_intent_only_confirms_offline(tmp_path: Path) -> None:
    calls: list[object] = []
    result = ExecutorRecovery(
        coordinator=_coordinator(calls),
        intents=VssIntentStore(tmp_path),
        vss_cleaner=_Cleaner(calls),
    ).run(job_id="job-1", disk=_disk())

    assert calls == ["lock", "inspect", "offline", "unlock"]
    assert result.vss_intent_cleaned is False


def test_vss_cleanup_failure_retains_intent_but_still_confirms_offline(tmp_path: Path) -> None:
    calls: list[object] = []
    store = _prepare_intent(tmp_path)

    with pytest.raises(LifecycleOperationError) as raised:
        ExecutorRecovery(
            coordinator=_coordinator(calls),
            intents=store,
            vss_cleaner=_Cleaner(calls, fail=True),
        ).run(job_id="job-1", disk=_disk())

    assert str(raised.value.primary_error) == "VSS cleanup failed"
    assert calls == ["lock", "inspect", ("delete", SET_ID), "offline", "unlock"]
    assert store.load("job-1") is not None
