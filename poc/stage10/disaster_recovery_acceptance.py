"""Prove recovery from repository plus independent config after total Stable loss."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backup_system.common.config import SnapshotJobConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.restic_auth import restic_auth_arguments
from backup_system.executor.restic_process import ResticProcess
from backup_system.executor.restore_request import RestoreRequest
from backup_system.executor.snapshot_adapter import SnapshotAdapter, SnapshotCursorResetWarning
from backup_system.executor.snapshot_restore import SnapshotRestore
from backup_system.executor.snapshot_state import SnapshotStateStore

RESULT_PATH = PROJECT_ROOT / ".poc-work" / "stage10" / "disaster-recovery-result.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _config(root: Path, passphrase: str) -> SnapshotJobConfig:
    return SnapshotJobConfig.model_validate(
        {
            "schema_version": 1,
            "id": "stage10-dr",
            "kind": "snapshot",
            "display_name": "Stage 10 disposable disaster recovery",
            "source": {"path": str(root / "lost-host" / "source")},
            "excludes": [],
            "repository": {
                "engine": "restic",
                "repository_id": "stage10-dr",
                "path": str(root / "backup-media" / "repository"),
                "marker_uuid": str(uuid4()),
                "encryption": {"mode": "password", "passphrase": passphrase},
                "marker_file": str(root / "backup-media" / ".backup-volume.json"),
            },
            "disk": {
                "physical_serial": "DISPOSABLE-STAGE10-DR",
                "expected_size_bytes": 1,
                "partition_guid": "test",
                "volume_guid": "test",
                "mount_point": str(root / "backup-media"),
                "repository_path_timeout_seconds": 30,
            },
            "backup": {
                "host": "stage10-lost-host",
                "tags": ["job:stage10-dr"],
                "read_error_result": "failed",
            },
            "retention": {
                "keep_last": 1,
                "keep_daily": 0,
                "keep_weekly": 0,
                "keep_monthly": 0,
                "keep_yearly": 0,
            },
            "verification": {"data_subset_parts": 4, "restore_test_paths": []},
        }
    )


def _auth(encryption: object, directory: Path):  # type: ignore[no-untyped-def]
    return restic_auth_arguments(encryption, directory, protect=lambda path: None)


def exercise(restic: Path, root: Path) -> dict[str, object]:
    passphrase = f"stage10-{uuid4()}"
    config = _config(root, passphrase)
    source = Path(config.source.path)
    repository = Path(config.repository.path)
    recovery_material = root / "recovery-material"
    source.mkdir(parents=True)
    repository.parent.mkdir(parents=True)
    recovery_material.mkdir()
    files = {
        "known.txt": b"known disaster recovery payload\n",
        "tree/second.bin": bytes(range(256)) * 4,
    }
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    config_path = recovery_material / "stage10-dr.json"
    config_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    runner = ResticProcess(restic, CancellationToken())
    with _auth(config.repository.encryption, root / "lost-host" / "secrets") as auth:
        runner.run(["--repo", str(repository), *auth, "init", "--json"], expect_json=True)
    backup = SnapshotAdapter(
        runner=runner,
        states=SnapshotStateStore(
            root / "lost-host" / "state", root / "lost-host" / "diagnostics"
        ),
        secret_directory=root / "lost-host" / "secrets",
        auth_factory=_auth,
    ).backup(config, source_root=source)
    expected = {relative: _sha256(source / relative) for relative in files}

    # Simulate complete loss of Stable, manager SQLite, runtime state, and source.
    shutil.rmtree(root / "lost-host")
    recovered_config = SnapshotJobConfig.model_validate_json(
        config_path.read_text(encoding="utf-8")
    )
    recovery_host = root / "recovery-host"
    restore_parent = recovery_host / "restores"
    restore_parent.mkdir(parents=True)
    recovery_runner = ResticProcess(restic, CancellationToken())
    recovery_adapter = SnapshotAdapter(
        runner=recovery_runner,
        states=SnapshotStateStore(recovery_host / "state", recovery_host / "diagnostics"),
        secret_directory=recovery_host / "secrets",
        auth_factory=_auth,
    )
    cursor_reset_reported = False
    try:
        recovery_adapter.check(recovered_config, mode="full")
    except SnapshotCursorResetWarning:
        # Losing manager/runtime state intentionally resets the scrub cursor. The
        # full repository read has completed before this operator-visible warning.
        cursor_reset_reported = True
    request = RestoreRequest.model_construct(
        schema_version=1,
        request_id=uuid4(),
        job_id=recovered_config.id,
        version=backup.snapshot_id,
        path=".",
        target=str(restore_parent),
    )
    restored = SnapshotRestore(
        runner=recovery_runner,
        cancellation=CancellationToken(),
        secret_directory=recovery_host / "secrets",
        auth_factory=_auth,
    ).run(recovered_config, request)
    actual = {relative: _sha256(restored.result_path / relative) for relative in files}
    if actual != expected:
        raise RuntimeError("disaster recovery content hash mismatch")
    if (root / "lost-host").exists():
        raise RuntimeError("lost host unexpectedly survived recovery simulation")
    return {
        "status": "passed",
        "repository_check": "full",
        "snapshot_id_length": len(backup.snapshot_id),
        "files_restored": restored.files_restored,
        "logical_bytes": restored.logical_bytes,
        "content_hashes_match": True,
        "manager_sqlite_used": False,
        "lost_host_used_for_restore": False,
        "cursor_reset_reported": cursor_reset_reported,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive", choices=("D",), default="D")
    args = parser.parse_args()
    root = Path(f"{args.drive}:\\bbs-stage10-dr-{uuid4()}")
    restic = PROJECT_ROOT / ".tools" / "restic-0.19.1" / "restic_0.19.1_windows_amd64.exe"
    result: dict[str, object]
    try:
        if not Path(f"{args.drive}:\\").is_dir():
            raise RuntimeError("disposable drive D is unavailable")
        if not restic.is_file():
            raise RuntimeError("pinned restic executable is missing")
        root.mkdir()
        result = exercise(restic, root)
    except Exception as error:
        result = {
            "status": "failed",
            "error": type(error).__name__,
            "diagnostic": str(error),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Result saved to: {RESULT_PATH}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
