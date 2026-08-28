from pathlib import PureWindowsPath
from uuid import UUID

import pytest

from backup_system.common.config import MirrorJobConfig
from backup_system.executor.disk_control import DiskCandidate
from backup_system.executor.mirror_runtime import (
    MirrorRuntimeError,
    _require_distinct_source_disk,
    _validated_destination,
)
from backup_system.executor.source_volume import ResolvedSourceVolume

MARKER = UUID("22222222-2222-4222-8222-222222222222")


def _config(destination: str) -> MirrorJobConfig:
    return MirrorJobConfig.model_validate(
        {
            "id": "job-1",
            "kind": "mirror",
            "display_name": "Mirror",
            "source": {"path": r"F:\Data"},
            "excludes": [],
            "disk": {
                "physical_serial": "serial",
                "expected_size_bytes": 1000,
                "partition_guid": "partition",
                "volume_guid": "volume",
                "mount_point": r"C:\BackupVolumes\primary",
                "repository_path_timeout_seconds": 30,
            },
            "destination": {
                "path": destination,
                "marker_file": r"C:\BackupVolumes\primary\.marker.json",
                "marker_uuid": str(MARKER),
            },
            "verification": {"restore_test_paths": []},
        }
    )


def test_destination_must_be_below_verified_mount() -> None:
    assert str(_validated_destination(_config(r"C:\BackupVolumes\primary\mirror"))).endswith(
        "mirror"
    )
    with pytest.raises(MirrorRuntimeError, match="verified"):
        _validated_destination(_config(r"D:\unrelated"))


def test_source_and_destination_physical_disks_must_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backup_system.executor.mirror_runtime as runtime

    volume_id = UUID("33333333-3333-4333-8333-333333333333")
    volume_name = "\\\\?\\Volume{" + str(volume_id) + "}\\"
    source = ResolvedSourceVolume(volume_id, volume_name, PureWindowsPath("."))
    candidate = DiskCandidate(
        disk_number=2,
        physical_serial="serial",
        size_bytes=1000,
        partition_guid="partition",
        volume_guid=str(volume_id),
        offline=False,
        is_boot=False,
        is_system=False,
    )
    monkeypatch.setattr(runtime.SourceVolumeResolver, "resolve", lambda self, path: source)
    monkeypatch.setattr(runtime.WindowsStorageInventory, "enumerate", lambda self: (candidate,))

    with pytest.raises(MirrorRuntimeError, match="same physical"):
        _require_distinct_source_disk(_config(r"C:\BackupVolumes\primary\mirror"))
