from uuid import UUID

import pytest

from backup_system.common.config import MirrorJobConfig
from backup_system.executor.mirror_runtime import (
    MirrorRuntimeError,
    _validated_destination,
)

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
