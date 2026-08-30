from uuid import uuid4

import pytest
from pydantic import ValidationError

from backup_system.common.config import (
    EXECUTOR_JOB_CONFIG_ADAPTER,
    ManagerConfig,
    MirrorJobConfig,
    SmartConfig,
    SmartTestJobConfig,
    SnapshotJobConfig,
)


def _disk() -> dict[str, object]:
    return {
        "physical_serial": "TEST-SERIAL",
        "expected_size_bytes": 1_000_000,
        "partition_guid": "partition-guid",
        "volume_guid": "volume-guid",
        "mount_point": r"C:\BackupVolumes\primary",
        "repository_path_timeout_seconds": 30,
    }


def _repository() -> dict[str, object]:
    return {
        "engine": "restic",
        "repository_id": "primary",
        "path": r"C:\Backup\restic",
        "marker_uuid": str(uuid4()),
        "encryption": {"mode": "none"},
        "marker_file": r"C:\Backup\.backup-volume.json",
    }


def test_snapshot_job_is_selected_by_discriminator() -> None:
    config = EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(
        {
            "schema_version": 1,
            "id": "data",
            "kind": "snapshot",
            "display_name": "Data",
            "source": {"path": "F:\\"},
            "excludes": ["System Volume Information", "$RECYCLE.BIN"],
            "repository": _repository(),
            "disk": _disk(),
            "backup": {"host": "test-host", "tags": ["job:data"], "read_error_result": "failed"},
            "retention": {
                "keep_last": 1,
                "keep_daily": 0,
                "keep_weekly": 4,
                "keep_monthly": 6,
                "keep_yearly": 0,
            },
            "verification": {"data_subset_parts": 4, "restore_test_paths": [r"F:\control"]},
        }
    )
    assert isinstance(config, SnapshotJobConfig)


@pytest.mark.parametrize(
    ("encryption", "valid"),
    [
        ({"mode": "none"}, True),
        ({"mode": "password", "passphrase": "test-only-secret"}, True),
        ({"mode": "password"}, False),
        ({"mode": "none", "passphrase": "unexpected"}, False),
        ({"mode": "environment"}, False),
    ],
)
def test_repository_encryption_contract(encryption: dict[str, str], valid: bool) -> None:
    repository = _repository()
    repository["encryption"] = encryption
    value = {
        "schema_version": 1,
        "id": "data",
        "kind": "snapshot",
        "display_name": "Data",
        "source": {"path": "F:\\"},
        "excludes": [],
        "repository": repository,
        "disk": _disk(),
        "backup": {"host": "test-host", "tags": ["job:data"], "read_error_result": "failed"},
        "retention": {
            "keep_last": 1,
            "keep_daily": 0,
            "keep_weekly": 4,
            "keep_monthly": 6,
            "keep_yearly": 0,
        },
        "verification": {"data_subset_parts": 4, "restore_test_paths": []},
    }
    if valid:
        EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(value)
    else:
        with pytest.raises(ValidationError):
            EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(value)


def test_mirror_job_rejects_snapshot_only_fields() -> None:
    value = {
        "schema_version": 1,
        "id": "data-mirror",
        "kind": "mirror",
        "display_name": "Data mirror",
        "source": {"path": "F:\\"},
        "excludes": [],
        "destination": {
            "path": r"C:\Backup\mirror",
            "marker_file": r"C:\Backup\mirror\.backup-system\marker.json",
            "marker_uuid": str(uuid4()),
        },
        "disk": _disk(),
        "verification": {"restore_test_paths": [r"F:\control"]},
        "retention": {"keep_last": 1},
    }
    with pytest.raises(ValidationError):
        EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(value)


def test_mirror_job_is_selected_by_discriminator() -> None:
    value = {
        "schema_version": 1,
        "id": "data-mirror",
        "kind": "mirror",
        "display_name": "Data mirror",
        "source": {"path": "F:\\"},
        "excludes": [],
        "destination": {
            "path": r"C:\Backup\mirror",
            "marker_file": r"C:\Backup\mirror\.backup-system\marker.json",
            "marker_uuid": str(uuid4()),
        },
        "disk": _disk(),
        "verification": {"restore_test_paths": [r"F:\control"]},
    }
    assert isinstance(EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(value), MirrorJobConfig)


def test_excludes_reject_case_insensitive_collisions() -> None:
    value = {
        "schema_version": 1,
        "id": "data-mirror",
        "kind": "mirror",
        "display_name": "Data mirror",
        "source": {"path": "F:\\"},
        "excludes": ["Cache", "cache"],
        "destination": {
            "path": r"C:\Backup\mirror",
            "marker_file": r"C:\Backup\mirror\.backup-system\marker.json",
            "marker_uuid": str(uuid4()),
        },
        "disk": _disk(),
        "verification": {"restore_test_paths": []},
    }
    with pytest.raises(ValidationError):
        EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(value)


def test_manager_config_rejects_invalid_cycle_mode() -> None:
    with pytest.raises(ValidationError):
        ManagerConfig.model_validate(
            {
                "schema_version": 1,
                "timezone": "Europe/Samara",
                "scheduler": {"poll_seconds": 5},
                "monitoring": {"volumes": {"poll_seconds": 60, "items": []}},
                "jobs": [
                    {
                        "id": "data",
                        "enabled": True,
                        "display_name": "Data",
                        "schedule": {
                            "cron": "0 0 * * 1",
                            "timezone": "Europe/Samara",
                            "deadline": "08:00",
                            "cycle": [{"operation": "backup", "mode": "full"}],
                        },
                    }
                ],
                "telegram": {
                    "enabled": False,
                    "credentials_file": "telegram.json",
                    "daily_report_cron": "0 9 * * *",
                    "daily_report_timezone": "Europe/Samara",
                    "stale_manager_minutes": 10,
                },
            }
        )


def test_smart_config_rejects_duplicate_serials() -> None:
    with pytest.raises(ValidationError):
        SmartConfig.model_validate(
            {
                "schema_version": 1,
                "per_disk_timeout_seconds": 30,
                "stale_after_hours": 48,
                "disks": [
                    {
                        "id": "source-main",
                        "display_name": "Source",
                        "identity": {
                            "device": "/dev/pd0",
                            "serial": "SERIAL",
                            "expected_size_bytes": 100,
                        },
                    },
                    {
                        "id": "backup-main",
                        "display_name": "Backup",
                        "identity": {
                            "device": "/dev/pd1",
                            "serial": "serial",
                            "expected_size_bytes": 200,
                        },
                    },
                ],
            }
        )


def test_smart_test_job_is_selected_by_discriminator() -> None:
    config = EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(
        {
            "schema_version": 1,
            "id": "test-disk-health",
            "kind": "smart-test",
            "display_name": "Test disk health",
            "target": {"mode": "all-system"},
            "test_type": "short",
            "poll_seconds": 30,
            "timeout_seconds": 900,
        }
    )
    assert isinstance(config, SmartTestJobConfig)
    assert config.target.mode == "all-system"
