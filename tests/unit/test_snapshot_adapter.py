from pathlib import Path
from uuid import uuid4

import pytest

from backup_system.common.config import EXECUTOR_JOB_CONFIG_ADAPTER, SnapshotJobConfig
from backup_system.executor.restic_process import ResticProcessError, ResticResult
from backup_system.executor.snapshot_adapter import (
    SnapshotAdapter,
    SnapshotVerificationRequired,
)
from backup_system.executor.snapshot_state import SnapshotStateStore


class FakeRunner:
    def __init__(self, results: list[ResticResult | BaseException]) -> None:
        self.results = results
        self.commands: list[tuple[str, ...]] = []
        self.version_checks = 0

    def verify_version(self) -> tuple[int, int, int]:
        self.version_checks += 1
        return (0, 19, 1)

    def run(self, arguments: object, *, expect_json: bool = True) -> ResticResult:
        del expect_json
        self.commands.append(tuple(arguments))  # type: ignore[arg-type]
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _config() -> SnapshotJobConfig:
    value = {
        "schema_version": 1,
        "id": "data",
        "kind": "snapshot",
        "display_name": "Data",
        "source": {"path": "F:\\"},
        "excludes": ["Cache"],
        "repository": {
            "engine": "restic",
            "repository_id": "primary",
            "path": r"C:\BackupVolumes\primary\restic",
            "marker_uuid": str(uuid4()),
            "encryption": {"mode": "none"},
            "marker_file": r"C:\BackupVolumes\primary\.backup-volume.json",
        },
        "disk": {
            "physical_serial": "TEST",
            "expected_size_bytes": 100,
            "partition_guid": "partition",
            "volume_guid": "volume",
            "mount_point": r"C:\BackupVolumes\primary",
            "repository_path_timeout_seconds": 30,
        },
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
    config = EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(value)
    assert isinstance(config, SnapshotJobConfig)
    return config


def _adapter(tmp_path: Path, runner: FakeRunner) -> SnapshotAdapter:
    return SnapshotAdapter(
        runner=runner,
        states=SnapshotStateStore(tmp_path / "state", tmp_path / "diagnostics"),
        secret_directory=tmp_path / "secrets",
    )


def test_backup_runs_snapshot_then_retention_then_observation(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            ResticResult(0, ({"message_type": "summary", "snapshot_id": "abc", "data_added": 7},)),
            ResticResult(0, ()),
            ResticResult(0, ({"id": "abcdef", "short_id": "abcdef"},)),
        ]
    )
    result = _adapter(tmp_path, runner).backup(_config(), source_root=tmp_path / "shadow")
    assert result.snapshot_id == "abc"
    assert result.snapshots == ("abcdef",)
    assert [command[3] for command in runner.commands] == ["backup", "forget", "snapshots"]
    assert "--keep-weekly" in runner.commands[1]


def test_failed_backup_never_runs_retention(tmp_path: Path) -> None:
    runner = FakeRunner([ResticProcessError("source_read_error", "failed")])
    with pytest.raises(ResticProcessError):
        _adapter(tmp_path, runner).backup(_config(), source_root=tmp_path / "shadow")
    assert len(runner.commands) == 1


def test_failed_check_repeats_cursor_and_blocks_backup(tmp_path: Path) -> None:
    runner = FakeRunner(
        [ResticProcessError("repository_io_error", "failed"), ResticResult(0, ())]
    )
    adapter = _adapter(tmp_path, runner)
    config = _config()
    with pytest.raises(ResticProcessError):
        adapter.check(config, mode="subset")
    with pytest.raises(SnapshotVerificationRequired):
        adapter.backup(config, source_root=tmp_path / "shadow")
    result = adapter.check(config, mode="subset")
    assert result.subset_part == 1


def test_only_successful_full_check_clears_gate(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            ResticProcessError("repository_io_error", "failed"),
            ResticResult(0, ()),
            ResticResult(0, ({"message_type": "summary", "snapshot_id": "new", "data_added": 1},)),
            ResticResult(0, ()),
            ResticResult(0, ()),
        ]
    )
    adapter = _adapter(tmp_path, runner)
    config = _config()
    with pytest.raises(ResticProcessError):
        adapter.check(config, mode="metadata")
    adapter.check(config, mode="full")
    assert adapter.backup(config, source_root=tmp_path / "shadow").snapshot_id == "new"
