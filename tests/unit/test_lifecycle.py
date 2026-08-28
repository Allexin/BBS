import json
from pathlib import Path
from uuid import uuid4

import pytest

from backup_system.common.config import DiskConfig
from backup_system.executor.disk_control import DiskObservation, VerifiedDisk, VolumeObservation
from backup_system.executor.lifecycle import (
    ExecutorDiskLifecycle,
    LifecycleCleanupError,
    MarkerExpectation,
    MarkerVerificationError,
    verify_marker,
)


def _config() -> DiskConfig:
    return DiskConfig(
        physical_serial="serial",
        expected_size_bytes=1000,
        partition_guid="partition",
        volume_guid="volume",
        mount_point=r"C:\BackupVolumes\primary",
        repository_path_timeout_seconds=30,
    )


def _verified() -> VerifiedDisk:
    return VerifiedDisk("serial", 1000, "partition", "volume")


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.inspect_error: BaseException | None = None
        self.offline_error: BaseException | None = None

    def inspect(self, config: DiskConfig) -> DiskObservation:
        self.calls.append("inspect")
        if self.inspect_error:
            raise self.inspect_error
        return DiskObservation(_verified(), True)

    def bring_online(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        self.calls.append("online")

    def ensure_repository_path(
        self, config: DiskConfig, verified_disk: VerifiedDisk
    ) -> VolumeObservation:
        self.calls.append("mount")
        return VolumeObservation("volume", config.mount_point, True)

    def take_offline(self, config: DiskConfig, verified_disk: VerifiedDisk) -> None:
        self.calls.append("offline")
        if self.offline_error:
            raise self.offline_error


def _marker() -> MarkerExpectation:
    return MarkerExpectation(r"C:\BackupVolumes\primary\.backup-volume.json", uuid4())


def test_successful_lifecycle_verifies_marker_before_action_and_offlines() -> None:
    control = FakeControl()
    marker_checks: list[MarkerExpectation] = []
    lifecycle = ExecutorDiskLifecycle(control, marker_verifier=marker_checks.append)
    result = lifecycle.run(_config(), _marker(), lambda volume: control.calls.append("action"))
    assert result.disk_offline_confirmed
    assert control.calls == ["inspect", "online", "mount", "action", "offline"]
    assert len(marker_checks) == 1


def test_primary_failure_is_preserved_when_cleanup_succeeds() -> None:
    control = FakeControl()

    def fail(marker: MarkerExpectation) -> None:
        raise MarkerVerificationError("wrong marker")

    with pytest.raises(MarkerVerificationError, match="wrong marker"):
        ExecutorDiskLifecycle(control, marker_verifier=fail).run(
            _config(), _marker(), lambda volume: None
        )
    assert control.calls[-1] == "offline"


def test_offline_failure_has_higher_severity_and_keeps_primary() -> None:
    control = FakeControl()
    control.offline_error = OSError("offline failed")
    primary = ValueError("data failed")

    def action(volume: VolumeObservation) -> None:
        raise primary

    with pytest.raises(LifecycleCleanupError) as captured:
        ExecutorDiskLifecycle(control, marker_verifier=lambda marker: None).run(
            _config(), _marker(), action
        )
    assert captured.value.primary_error is primary


def test_identity_failure_never_attempts_offline_or_online() -> None:
    control = FakeControl()
    control.inspect_error = MarkerVerificationError("identity")
    with pytest.raises(MarkerVerificationError):
        ExecutorDiskLifecycle(control).run(_config(), _marker(), lambda volume: None)
    assert control.calls == ["inspect"]


def test_recover_only_inspects_and_confirms_offline() -> None:
    control = FakeControl()
    ExecutorDiskLifecycle(control).recover(_config())
    assert control.calls == ["inspect", "offline"]


def test_recover_runs_owned_vss_cleanup_before_disk_offline() -> None:
    control = FakeControl()
    ExecutorDiskLifecycle(control).recover(
        _config(), pre_offline_cleanup=lambda: control.calls.append("vss-cleanup")
    )
    assert control.calls == ["inspect", "vss-cleanup", "offline"]


def test_recover_still_offlines_disk_after_vss_cleanup_failure() -> None:
    control = FakeControl()
    primary = RuntimeError("VSS cleanup failed")

    def fail_cleanup() -> None:
        raise primary

    with pytest.raises(RuntimeError, match="VSS cleanup failed") as raised:
        ExecutorDiskLifecycle(control).recover(_config(), pre_offline_cleanup=fail_cleanup)
    assert raised.value is primary
    assert control.calls == ["inspect", "offline"]


def test_recover_offline_failure_keeps_vss_cleanup_failure() -> None:
    control = FakeControl()
    control.offline_error = OSError("offline failed")
    primary = RuntimeError("VSS cleanup failed")

    def fail_cleanup() -> None:
        raise primary

    with pytest.raises(LifecycleCleanupError) as raised:
        ExecutorDiskLifecycle(control).recover(_config(), pre_offline_cleanup=fail_cleanup)
    assert raised.value.primary_error is primary


def test_marker_verification_is_bounded_and_exact(tmp_path: Path) -> None:
    marker_uuid = uuid4()
    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"marker_uuid": str(marker_uuid)}), encoding="utf-8")
    verify_marker(MarkerExpectation(str(marker), marker_uuid))
    with pytest.raises(MarkerVerificationError, match="does not match"):
        verify_marker(MarkerExpectation(str(marker), uuid4()))


def test_marker_outside_mount_is_rejected_before_inspection() -> None:
    control = FakeControl()
    marker = MarkerExpectation(r"C:\Other\marker.json", uuid4())
    with pytest.raises(MarkerVerificationError, match="inside"):
        ExecutorDiskLifecycle(control).run(_config(), marker, lambda volume: None)
    assert control.calls == []
