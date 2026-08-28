from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backup_system.common.config import SnapshotJobConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.restic_process import ResticResult
from backup_system.executor.restore_request import RestoreRequest
from backup_system.executor.restore_target import RestoreTarget, RestoreVerificationError
from backup_system.executor.snapshot_restore import SnapshotRestore

SNAPSHOT_ID = "a" * 64
REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def verify_version(self) -> tuple[int, int, int]:
        return (0, 19, 1)

    def run(self, arguments: object, *, expect_json: bool = True) -> ResticResult:
        del expect_json
        command = tuple(arguments)  # type: ignore[arg-type]
        self.commands.append(command)
        if "snapshots" in command:
            return ResticResult(
                0,
                ({"id": SNAPSHOT_ID, "short_id": "aaaaaaaa", "time": "2026-01-01"},),
            )
        target = Path(command[command.index("--target") + 1])
        source = target / "source"
        (source / "Folder").mkdir(parents=True)
        (source / "Folder" / "file.txt").write_bytes(b"snapshot-data")
        (source / "root.txt").write_bytes(b"root")
        return ResticResult(0, ())


def _config() -> SnapshotJobConfig:
    return SnapshotJobConfig.model_validate(
        {
            "id": "data",
            "kind": "snapshot",
            "display_name": "Data",
            "source": {"path": r"F:\source"},
            "excludes": [],
            "repository": {
                "engine": "restic",
                "repository_id": "repo",
                "path": r"D:\backup\restic",
                "marker_uuid": str(uuid4()),
                "encryption": {"mode": "none"},
                "marker_file": r"D:\backup\.marker.json",
            },
            "disk": {
                "physical_serial": "TEST",
                "expected_size_bytes": 100,
                "partition_guid": "partition",
                "volume_guid": "volume",
                "mount_point": r"D:\backup",
                "repository_path_timeout_seconds": 30,
            },
            "backup": {"host": "host", "tags": ["job:data"], "read_error_result": "failed"},
            "retention": {
                "keep_last": 1,
                "keep_daily": 0,
                "keep_weekly": 4,
                "keep_monthly": 6,
                "keep_yearly": 0,
            },
            "verification": {"data_subset_parts": 4, "restore_test_paths": []},
        }
    )


def _request(target: Path, selection: str, version: str = "latest") -> RestoreRequest:
    return RestoreRequest.model_construct(
        schema_version=1,
        request_id=REQUEST_ID,
        job_id="data",
        version=version,
        path=selection,
        target=str(target),
    )


def _adapter(runner: FakeRunner, tmp_path: Path) -> SnapshotRestore:
    return SnapshotRestore(
        runner=runner,
        cancellation=CancellationToken(),
        secret_directory=tmp_path / "secrets",
    )


def test_snapshot_subtree_restore_preserves_layout(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    runner = FakeRunner()
    progress: list[tuple[str, int]] = []
    outcome = SnapshotRestore(
        runner=runner,
        cancellation=CancellationToken(),
        secret_directory=tmp_path / "secrets",
        progress_sink=lambda stage, done, total, bytes_done, bytes_total: progress.append(
            (stage, done)
        ),
    ).run(_config(), _request(target, "Folder"))
    assert (outcome.result_path / "Folder" / "file.txt").read_bytes() == b"snapshot-data"
    assert not (outcome.result_path / "root.txt").exists()
    restore_command = runner.commands[1]
    assert SNAPSHOT_ID in restore_command
    assert "--verify" in restore_command
    assert "--overwrite" in restore_command and "never" in restore_command
    assert progress == [("restoring", 1), ("verifying", 1)]


def test_explicit_snapshot_must_belong_to_filtered_job_list(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(Exception, match="does not uniquely belong"):
        _adapter(FakeRunner(), tmp_path).run(_config(), _request(target, "root.txt", "b" * 64))
    result = next(target.glob("BackupRestore-*"))
    assert (result / ".restore-incomplete").exists()


def test_post_restore_tampering_cannot_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    original = RestoreTarget.verify_and_complete

    def tamper(self: RestoreTarget, result: Path, manifest: object):
        (result / "root.txt").write_bytes(b"tampered")
        return original(self, result, manifest)  # type: ignore[arg-type]

    monkeypatch.setattr(RestoreTarget, "verify_and_complete", tamper)
    with pytest.raises(RestoreVerificationError):
        _adapter(FakeRunner(), tmp_path).run(_config(), _request(target, "root.txt"))
    result = next(target.glob("BackupRestore-*"))
    assert (result / ".restore-incomplete").exists()
