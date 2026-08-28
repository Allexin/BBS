from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import PureWindowsPath
from typing import TypeVar
from uuid import UUID

import pytest

from backup_system.common.config import (
    DiskConfig,
    MirrorJobConfig,
    SmartConfig,
    SnapshotJobConfig,
)
from backup_system.common.smart import SmartMetrics
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.coordinator import ExecutorWindowsCoordinator
from backup_system.executor.disk_control import DiskObservation, VerifiedDisk, VolumeObservation
from backup_system.executor.lifecycle import ExecutorDiskLifecycle, LifecycleOperationError
from backup_system.executor.smart_preflight import SmartPreflightObservation
from backup_system.executor.windows_job import (
    ExecutorWindowsJob,
    WindowsDataContext,
    marker_expectation,
)

T = TypeVar("T")
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
MARKER_ID = UUID("22222222-2222-4222-8222-222222222222")


def _disk() -> DiskConfig:
    return DiskConfig(
        physical_serial="serial",
        expected_size_bytes=1000,
        partition_guid="partition",
        volume_guid="volume",
        mount_point=r"C:\BackupVolumes\primary",
        repository_path_timeout_seconds=30,
    )


def _snapshot() -> SnapshotJobConfig:
    return SnapshotJobConfig.model_validate(
        {
            "id": "job-1",
            "kind": "snapshot",
            "display_name": "Job 1",
            "source": {"path": "F:\\Data"},
            "excludes": [],
            "disk": _disk().model_dump(),
            "repository": {
                "engine": "restic",
                "repository_id": "repo-1",
                "path": r"C:\BackupVolumes\primary\repo",
                "marker_uuid": str(MARKER_ID),
                "encryption": {"mode": "none"},
                "marker_file": r"C:\BackupVolumes\primary\.marker.json",
            },
            "backup": {"host": "host", "tags": [], "read_error_result": "failed"},
            "retention": {
                "keep_last": 1,
                "keep_daily": 0,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
            },
            "verification": {"restore_test_paths": [], "data_subset_parts": 4},
        }
    )


class _Control:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    def inspect(self, config: DiskConfig) -> DiskObservation:
        self._calls.append("inspect")
        return DiskObservation(VerifiedDisk("serial", 1000, "partition", "volume"), True)

    def bring_online(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        self._calls.append("online")

    def ensure_repository_path(
        self, config: DiskConfig, verified_disk: VerifiedDisk
    ) -> VolumeObservation:
        self._calls.append("mount")
        return VolumeObservation("volume", config.mount_point, True)

    def take_offline(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        self._calls.append("offline")


class _Smart:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    def collect(self, config: SmartConfig) -> tuple[SmartPreflightObservation, ...]:
        self._calls.append("smart")
        return (SmartPreflightObservation("disk-1", True, "healthy", SmartMetrics()),)


class _Snapshots:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    def run(
        self,
        *,
        job_id: str,
        run_id: UUID,
        source_path: str,
        action: Callable[[PureWindowsPath], T],
    ) -> T:
        self._calls.append(("snapshot", job_id, run_id, source_path))
        try:
            return action(PureWindowsPath(r"\\?\shadow\Data"))
        finally:
            self._calls.append("snapshot-cleanup")


def _smart_config() -> SmartConfig:
    return SmartConfig.model_validate(
        {
            "per_disk_timeout_seconds": 5,
            "stale_after_hours": 24,
            "disks": [],
        }
    )


def _runtime(calls: list[object]) -> ExecutorWindowsJob:
    @contextmanager
    def lock() -> Iterator[object]:
        calls.append("lock")
        try:
            yield object()
        finally:
            calls.append("unlock")

    coordinator = ExecutorWindowsCoordinator(
        lock_factory=lock,
        disk_lifecycle=ExecutorDiskLifecycle(
            _Control(calls), marker_verifier=lambda marker: calls.append(("marker", marker))
        ),
        smart=_Smart(calls),
        smart_sink=lambda observations: calls.append("smart-events"),
        cancellation=CancellationToken(),
    )
    return ExecutorWindowsJob(coordinator=coordinator, source_snapshots=_Snapshots(calls))


def test_pipeline_orders_backup_disk_smart_vss_adapter_and_cleanup() -> None:
    calls: list[object] = []
    config = _snapshot()

    result = _runtime(calls).run(
        config=config,
        smart_config=_smart_config(),
        run_id=RUN_ID,
        adapter=lambda context: calls.append(("adapter", context)) or "done",
    )

    assert result.value == "done"
    assert calls == [
        "lock",
        "inspect",
        "online",
        "mount",
        ("marker", marker_expectation(config)),
        "smart",
        "smart-events",
        ("snapshot", "job-1", RUN_ID, r"F:\Data"),
        (
            "adapter",
            WindowsDataContext(
                PureWindowsPath(r"\\?\shadow\Data"),
                VolumeObservation("volume", r"C:\BackupVolumes\primary", True),
            ),
        ),
        "snapshot-cleanup",
        "offline",
        "unlock",
    ]


def test_marker_is_selected_from_job_kind() -> None:
    snapshot = _snapshot()
    assert marker_expectation(snapshot).file == snapshot.repository.marker_file

    payload = snapshot.model_dump()
    payload["kind"] = "mirror"
    payload.pop("repository")
    payload.pop("backup")
    payload.pop("retention")
    payload["destination"] = {
        "path": r"C:\BackupVolumes\primary\mirror",
        "marker_file": r"C:\BackupVolumes\primary\mirror\.marker.json",
        "marker_uuid": str(MARKER_ID),
    }
    payload["verification"] = {"restore_test_paths": []}
    mirror = MirrorJobConfig.model_validate(payload)
    assert marker_expectation(mirror).file == mirror.destination.marker_file


def test_adapter_failure_cleans_snapshot_then_returns_disk_offline() -> None:
    calls: list[object] = []
    failure = ValueError("adapter failed")

    def fail(context: WindowsDataContext) -> None:
        calls.append("adapter")
        raise failure

    with pytest.raises(LifecycleOperationError) as raised:
        _runtime(calls).run(
            config=_snapshot(),
            smart_config=_smart_config(),
            run_id=RUN_ID,
            adapter=fail,
        )

    assert raised.value.primary_error is failure
    assert calls[-4:] == ["adapter", "snapshot-cleanup", "offline", "unlock"]
