from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from backup_system.common.config import (
    DiskConfig,
    SmartConfig,
    SmartDiskConfig,
    SmartDiskIdentityConfig,
)
from backup_system.common.smart import SmartMetrics
from backup_system.executor.coordinator import ExecutorWindowsCoordinator
from backup_system.executor.disk_control import DiskObservation, VerifiedDisk, VolumeObservation
from backup_system.executor.lifecycle import ExecutorDiskLifecycle, MarkerExpectation
from backup_system.executor.smart_preflight import SmartPreflightObservation


def _disk() -> DiskConfig:
    return DiskConfig(
        physical_serial="serial",
        expected_size_bytes=1000,
        partition_guid="partition",
        volume_guid="volume",
        mount_point=r"C:\BackupVolumes\primary",
        repository_path_timeout_seconds=30,
    )


def _smart_config() -> SmartConfig:
    return SmartConfig(
        per_disk_timeout_seconds=10,
        stale_after_hours=24,
        disks=(
            SmartDiskConfig(
                id="disk-1",
                display_name="Disk 1",
                identity=SmartDiskIdentityConfig(serial="serial", expected_size_bytes=1000),
            ),
        ),
    )


class Control:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def inspect(self, config: DiskConfig) -> DiskObservation:
        self.calls.append("inspect")
        return DiskObservation(VerifiedDisk("serial", 1000, "partition", "volume"), True)

    def bring_online(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        self.calls.append("online")

    def ensure_repository_path(
        self, config: DiskConfig, verified_disk: VerifiedDisk
    ) -> VolumeObservation:
        self.calls.append("mount")
        return VolumeObservation("volume", config.mount_point, True)

    def take_offline(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        self.calls.append("offline")


class Smart:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def collect(self, config: SmartConfig) -> tuple[SmartPreflightObservation, ...]:
        self.calls.append("smart")
        return (SmartPreflightObservation("disk-1", True, "healthy", SmartMetrics()),)


def _coordinator(
    calls: list[str], sink: list[tuple[SmartPreflightObservation, ...]]
) -> ExecutorWindowsCoordinator:
    @contextmanager
    def lock() -> Iterator[object]:
        calls.append("lock")
        try:
            yield object()
        finally:
            calls.append("unlock")

    return ExecutorWindowsCoordinator(
        lock_factory=lock,
        disk_lifecycle=ExecutorDiskLifecycle(
            Control(calls), marker_verifier=lambda marker: calls.append("marker")
        ),
        smart=Smart(calls),
        smart_sink=sink.append,
    )


def test_run_orders_lock_identity_mount_smart_action_and_offline() -> None:
    calls: list[str] = []
    sink: list[tuple[SmartPreflightObservation, ...]] = []
    result = _coordinator(calls, sink).run(
        disk=_disk(),
        marker=MarkerExpectation(r"C:\BackupVolumes\primary\.backup-volume.json", UUID(int=1)),
        smart_config=_smart_config(),
        action=lambda volume: calls.append("action") or "done",
    )
    assert calls == [
        "lock",
        "inspect",
        "online",
        "mount",
        "marker",
        "smart",
        "action",
        "offline",
        "unlock",
    ]
    assert result.value == "done"
    assert result.disk_offline_confirmed
    assert result.smart == sink[0]


def test_recover_holds_lock_across_vss_cleanup_and_offline() -> None:
    calls: list[str] = []
    _coordinator(calls, []).recover(
        disk=_disk(), owned_vss_cleanup=lambda: calls.append("vss-cleanup")
    )
    assert calls == ["lock", "inspect", "vss-cleanup", "offline", "unlock"]
