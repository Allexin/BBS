import pytest

from backup_system.common.config import (
    DestinationConfig,
    DiskConfig,
    EncryptionConfig,
    MaintenanceJobConfig,
    MirrorJobConfig,
    RepositoryConfig,
    SnapshotBackupConfig,
    SnapshotJobConfig,
    SnapshotRetentionConfig,
    SnapshotVerificationConfig,
    SourceConfig,
    VerificationConfig,
)
from backup_system.executor.operation_policy import (
    OperationNotAllowedError,
    require_operation_allowed,
)


def _disk() -> DiskConfig:
    return DiskConfig(
        physical_serial="serial",
        expected_size_bytes=1000,
        partition_guid="partition",
        volume_guid="volume",
        mount_point=r"C:\BackupVolumes\primary",
        repository_path_timeout_seconds=30,
    )


def _repository() -> RepositoryConfig:
    return RepositoryConfig(
        engine="restic",
        repository_id="repository-1",
        path=r"C:\BackupVolumes\primary\repository",
        marker_uuid="11111111-1111-4111-8111-111111111111",
        marker_file=r"C:\BackupVolumes\primary\.marker.json",
        encryption=EncryptionConfig(mode="none"),
    )


def _snapshot() -> SnapshotJobConfig:
    return SnapshotJobConfig(
        id="snapshot-1",
        kind="snapshot",
        display_name="Snapshot",
        source=SourceConfig(path=r"C:\Source"),
        excludes=(),
        disk=_disk(),
        repository=_repository(),
        backup=SnapshotBackupConfig(host="host", tags=(), read_error_result="failed"),
        retention=SnapshotRetentionConfig(
            keep_last=1, keep_daily=1, keep_weekly=1, keep_monthly=1, keep_yearly=1
        ),
        verification=SnapshotVerificationConfig(
            restore_test_paths=(r"C:\Source\control",), data_subset_parts=1
        ),
    )


def _mirror() -> MirrorJobConfig:
    return MirrorJobConfig(
        id="mirror-1",
        kind="mirror",
        display_name="Mirror",
        source=SourceConfig(path=r"C:\Source"),
        excludes=(),
        disk=_disk(),
        destination=DestinationConfig(
            path=r"C:\BackupVolumes\primary\mirror",
            marker_file=r"C:\BackupVolumes\primary\.marker.json",
            marker_uuid="22222222-2222-4222-8222-222222222222",
        ),
        verification=VerificationConfig(restore_test_paths=(r"C:\Source\control",)),
    )


def _maintenance() -> MaintenanceJobConfig:
    return MaintenanceJobConfig(
        id="maintenance-1",
        kind="maintenance",
        display_name="Maintenance",
        repository_owner_job_id="snapshot-1",
        repository=_repository(),
        disk=_disk(),
    )


@pytest.mark.parametrize(
    ("config", "allowed"),
    [
        (_snapshot(), {"run", "check", "restore", "restore-test", "recover"}),
        (
            _mirror(),
            {"run", "check", "restore", "restore-test", "repair-mirror", "recover"},
        ),
        (_maintenance(), {"prune", "recover"}),
    ],
)
def test_operation_matrix_is_exact(config: object, allowed: set[str]) -> None:
    operations = {"run", "check", "prune", "restore", "restore-test", "repair-mirror", "recover"}
    for operation in operations:
        if operation in allowed:
            require_operation_allowed(config, operation)  # type: ignore[arg-type]
        else:
            with pytest.raises(OperationNotAllowedError):
                require_operation_allowed(config, operation)  # type: ignore[arg-type]
