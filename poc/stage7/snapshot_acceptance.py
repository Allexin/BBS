"""Guarded stage-7 acceptance against disposable drive D only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backup_system.common.config import EXECUTOR_JOB_CONFIG_ADAPTER, SnapshotJobConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.restic_auth import restic_auth_arguments
from backup_system.executor.restic_process import ResticProcess, ResticProcessError
from backup_system.executor.snapshot_adapter import SnapshotAdapter, SnapshotVerificationRequired
from backup_system.executor.snapshot_state import SnapshotStateStore

RESULT_PATH = PROJECT_ROOT / ".poc-work" / "stage7" / "snapshot-hardware-result.json"


def config_for(root: Path, *, mode: str, passphrase: str | None) -> SnapshotJobConfig:
    encryption: dict[str, str] = {"mode": mode}
    if passphrase is not None:
        encryption["passphrase"] = passphrase
    value = {
        "schema_version": 1,
        "id": f"stage7-{mode}",
        "kind": "snapshot",
        "display_name": "Stage 7 disposable acceptance",
        "source": {"path": str(root / "source")},
        "excludes": ["excluded"],
        "repository": {
            "engine": "restic",
            "repository_id": f"stage7-{mode}",
            "path": str(root / "repository"),
            "marker_uuid": str(uuid4()),
            "encryption": encryption,
            "marker_file": str(root / "marker.json"),
        },
        "disk": {
            "physical_serial": "DISPOSABLE-STAGE7",
            "expected_size_bytes": 1,
            "partition_guid": "test",
            "volume_guid": "test",
            "mount_point": str(root),
            "repository_path_timeout_seconds": 30,
        },
        "backup": {
            "host": "stage7-test-host",
            "tags": [f"job:stage7-{mode}"],
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
    config = EXECUTOR_JOB_CONFIG_ADAPTER.validate_python(value)
    assert isinstance(config, SnapshotJobConfig)
    return config


def exercise(restic: Path, root: Path, *, mode: str, passphrase: str | None) -> dict[str, object]:
    config = config_for(root, mode=mode, passphrase=passphrase)
    source = root / "source"
    source.mkdir(parents=True)
    (source / "unicode-данные.txt").write_text("first version", encoding="utf-8")
    (source / "excluded").mkdir()
    (source / "excluded" / "ignored.txt").write_text("excluded", encoding="utf-8")
    runner = ResticProcess(restic, CancellationToken())
    with restic_auth_arguments(
        config.repository.encryption, root / "secrets", protect=lambda path: None
    ) as auth:
        runner.run(
            ["--repo", config.repository.path, *auth, "init", "--json"],
            expect_json=True,
        )
    adapter = SnapshotAdapter(
        runner=runner,
        states=SnapshotStateStore(root / "state", root / "diagnostics"),
        secret_directory=root / "secrets",
        auth_factory=lambda encryption, directory: restic_auth_arguments(
            encryption, directory, protect=lambda path: None
        ),
    )
    first = adapter.backup(config, source_root=source)
    (source / "unicode-данные.txt").write_text("second version", encoding="utf-8")
    second = adapter.backup(config, source_root=source)
    subset = adapter.check(config, mode="subset")
    adapter.prune(_maintenance(config))
    packs = sorted((root / "repository" / "data").glob("*/*"))
    if not packs:
        raise RuntimeError("restic repository contains no pack to corrupt")
    with packs[0].open("r+b") as stream:
        first_byte = stream.read(1)
        stream.seek(0)
        stream.write(bytes([first_byte[0] ^ 0xFF]))
    corruption_detected = False
    try:
        adapter.check(config, mode="full")
    except ResticProcessError:
        corruption_detected = True
    if not corruption_detected:
        raise RuntimeError("full check did not detect repository corruption")
    gate_blocked = False
    try:
        adapter.backup(config, source_root=source)
    except SnapshotVerificationRequired:
        gate_blocked = True
    if not gate_blocked:
        raise RuntimeError("verification gate did not block backup")
    return {
        "mode": mode,
        "first_snapshot": first.snapshot_id,
        "second_snapshot": second.snapshot_id,
        "retained_snapshots": len(second.snapshots),
        "subset": f"{subset.subset_part}/{subset.subset_parts}",
        "prune": "passed",
        "corruption_detected": corruption_detected,
        "verification_gate_blocked_backup": gate_blocked,
    }


def _maintenance(config: SnapshotJobConfig):
    from backup_system.common.config import MaintenanceJobConfig

    return MaintenanceJobConfig.model_validate(
        {
            "id": f"{config.id}-maintenance",
            "kind": "maintenance",
            "display_name": "Stage 7 maintenance",
            "repository_owner_job_id": config.id,
            "repository": config.repository.model_dump(),
            "disk": config.disk.model_dump(),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive", required=True)
    args = parser.parse_args()
    if args.drive.upper().rstrip(":\\") != "D":
        raise RuntimeError("stage 7 acceptance is restricted to disposable drive D")
    drive = Path("D:/")
    if not drive.is_dir():
        raise RuntimeError("disposable drive D is not mounted")
    restic = PROJECT_ROOT / ".tools" / "restic-0.19.1" / "restic_0.19.1_windows_amd64.exe"
    if not restic.is_file():
        raise RuntimeError("pinned restic executable is missing")
    work = drive / f"bbs-stage7-{uuid4()}"
    try:
        work.mkdir()
        modes = [
            exercise(restic, work / "none", mode="none", passphrase=None),
            exercise(restic, work / "password", mode="password", passphrase="stage7-test-only"),
        ]
        result = {"status": "passed", "drive": "D", "restic": "0.19.1", "modes": modes}
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Result saved to: {RESULT_PATH}")
        return 0
    finally:
        if work.parent == drive and work.name.startswith("bbs-stage7-"):
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "error": type(error).__name__,
                    "detail": str(error),
                    "cause": type(error.__cause__).__name__ if error.__cause__ else None,
                    "cause_detail": str(error.__cause__) if error.__cause__ else None,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Stage 7 acceptance failed; result saved to: {RESULT_PATH}", file=sys.stderr)
        raise SystemExit(1) from error
