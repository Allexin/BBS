"""Guarded stage-8 restore acceptance using disposable drive D only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backup_system.common.config import SnapshotJobConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_catalog import MirrorCatalog
from backup_system.executor.mirror_restore import MirrorRestore
from backup_system.executor.restic_process import ResticProcess
from backup_system.executor.restore_request import RestoreRequest
from backup_system.executor.restore_target import RestoreVerificationError
from backup_system.executor.snapshot_adapter import SnapshotAdapter
from backup_system.executor.snapshot_restore import SnapshotRestore
from backup_system.executor.snapshot_state import SnapshotStateStore

RESULT_PATH = PROJECT_ROOT / ".poc-work" / "stage8" / "restore-hardware-result.json"


def snapshot_config(root: Path) -> SnapshotJobConfig:
    return SnapshotJobConfig.model_validate(
        {
            "id": "stage8-snapshot",
            "kind": "snapshot",
            "display_name": "Stage 8 snapshot",
            "source": {"path": str(root / "source")},
            "excludes": [],
            "repository": {
                "engine": "restic",
                "repository_id": "stage8",
                "path": str(root / "repository"),
                "marker_uuid": str(uuid4()),
                "encryption": {"mode": "none"},
                "marker_file": str(root / "marker.json"),
            },
            "disk": {
                "physical_serial": "DISPOSABLE-STAGE8",
                "expected_size_bytes": 1,
                "partition_guid": "test",
                "volume_guid": "test",
                "mount_point": str(root),
                "repository_path_timeout_seconds": 30,
            },
            "backup": {
                "host": "stage8-test-host",
                "tags": ["job:stage8-snapshot"],
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


def request(job_id: str, target: Path, selection: str, version: str = "latest") -> RestoreRequest:
    return RestoreRequest.model_construct(
        schema_version=1,
        request_id=uuid4(),
        job_id=job_id,
        version=version,
        path=selection,
        target=str(target),
    )


def exercise_snapshot(restic: Path, root: Path) -> dict[str, object]:
    config = snapshot_config(root)
    source = Path(config.source.path)
    target = root / "restores"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "single.txt").write_bytes(b"single")
    (source / "tree").mkdir()
    (source / "tree" / "one.txt").write_bytes(b"one")
    (source / "tree" / "two.txt").write_bytes(b"two")
    runner = ResticProcess(restic, CancellationToken())
    runner.run(
        ["--repo", config.repository.path, "--insecure-no-password", "init", "--json"]
    )
    backup = SnapshotAdapter(
        runner=runner,
        states=SnapshotStateStore(root / "state", root / "diagnostics"),
        secret_directory=root / "secrets",
    ).backup(config, source_root=source)
    adapter = SnapshotRestore(
        runner=runner,
        cancellation=CancellationToken(),
        secret_directory=root / "secrets",
    )
    single = adapter.run(config, request(config.id, target, "single.txt", backup.snapshot_id))
    subtree = adapter.run(config, request(config.id, target, "tree"))
    whole = adapter.run(config, request(config.id, target, "."))
    if (single.result_path / "single.txt").read_bytes() != b"single":
        raise RuntimeError("snapshot single-file restore mismatch")
    if sorted(path.name for path in (subtree.result_path / "tree").iterdir()) != [
        "one.txt",
        "two.txt",
    ]:
        raise RuntimeError("snapshot subtree restore mismatch")
    if not (whole.result_path / "tree" / "two.txt").is_file():
        raise RuntimeError("snapshot whole-source restore mismatch")
    pending_result: list[Path] = []

    def tamper_at_verification(stage: str) -> None:
        if stage == "verifying":
            (pending_result[0] / "single.txt").write_bytes(b"silent-corruption")

    tampering_detected = False
    try:
        SnapshotRestore(
            runner=runner,
            cancellation=CancellationToken(),
            secret_directory=root / "secrets",
            ready_sink=pending_result.append,
            stage_sink=tamper_at_verification,
        ).run(config, request(config.id, target, "single.txt"))
    except RestoreVerificationError:
        tampering_detected = True
    if not tampering_detected or not (pending_result[0] / ".restore-incomplete").exists():
        raise RuntimeError("snapshot post-restore corruption was not retained as incomplete")
    return {
        "single": "passed",
        "subtree": "passed",
        "whole": "passed",
        "silent_corruption_detected": True,
        "incomplete_marker_retained": True,
    }


def exercise_mirror(root: Path) -> dict[str, object]:
    mirror = root / "mirror"
    source = root / "original"
    target = root / "restores"
    source.mkdir(parents=True)
    target.mkdir()
    files = {"single.txt": b"single", "tree/one.txt": b"one", "tree/two.txt": b"two"}
    marker_uuid = uuid4()
    with MirrorCatalog(
        mirror / ".backup-system" / "catalog.sqlite3",
        job_id="stage8-mirror",
        marker_uuid=marker_uuid,
    ) as catalog:
        for index, (relative, content) in enumerate(files.items()):
            path = mirror / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            key = relative.upper()
            catalog.accept_present(
                path_key=key,
                relative_path=relative,
                size_bytes=len(content),
                source_mtime_ns=index,
                sha256=hashlib.sha256(content).digest(),
                temp_relative_path=f"temp-{index}",
                generation_id=uuid4(),
            )
            catalog.clear_temp(key)

    def copy(source_path: Path, destination: Path, size: int) -> None:
        if source_path.stat().st_size != size:
            raise RuntimeError("mirror source size changed")
        with source_path.open("rb") as input_stream, destination.open("xb") as output:
            shutil.copyfileobj(input_stream, output)

    adapter = MirrorRestore(cancellation=CancellationToken(), copy_file=copy)
    single = adapter.run(
        destination_root=mirror,
        source_root=source,
        request=request("stage8-mirror", target, "single.txt"),
        job_id="stage8-mirror",
        marker_uuid=marker_uuid,
    )
    subtree = adapter.run(
        destination_root=mirror,
        source_root=source,
        request=request("stage8-mirror", target, "tree"),
        job_id="stage8-mirror",
        marker_uuid=marker_uuid,
    )
    whole = adapter.run(
        destination_root=mirror,
        source_root=source,
        request=request("stage8-mirror", target, "."),
        job_id="stage8-mirror",
        marker_uuid=marker_uuid,
    )
    if (single.result_path / "single.txt").read_bytes() != b"single":
        raise RuntimeError("mirror single-file restore mismatch")
    if not (subtree.result_path / "tree" / "two.txt").is_file():
        raise RuntimeError("mirror subtree restore mismatch")
    if len(list(whole.result_path.rglob("*.txt"))) != 3:
        raise RuntimeError("mirror whole-source restore mismatch")

    def corrupt(source_path: Path, destination: Path, size: int) -> None:
        del source_path, size
        destination.write_bytes(b"silent-corruption")

    corruption_detected = False
    try:
        MirrorRestore(cancellation=CancellationToken(), copy_file=corrupt).run(
            destination_root=mirror,
            source_root=source,
            request=request("stage8-mirror", target, "single.txt"),
            job_id="stage8-mirror",
            marker_uuid=marker_uuid,
        )
    except RestoreVerificationError:
        corruption_detected = True
    if not corruption_detected:
        raise RuntimeError("silent restore corruption was not detected")
    incomplete = [path for path in target.glob("BackupRestore-*") if (path / ".restore-incomplete").exists()]
    if len(incomplete) != 1:
        raise RuntimeError("failed restore did not retain exactly one incomplete marker")
    return {
        "single": "passed",
        "subtree": "passed",
        "whole": "passed",
        "silent_corruption_detected": True,
        "incomplete_marker_retained": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive", required=True)
    args = parser.parse_args()
    if args.drive.upper().rstrip(":\\") != "D":
        raise RuntimeError("stage 8 acceptance is restricted to disposable drive D")
    drive = Path("D:/")
    if not drive.is_dir():
        raise RuntimeError("disposable drive D is not mounted")
    restic = PROJECT_ROOT / ".tools" / "restic-0.19.1" / "restic_0.19.1_windows_amd64.exe"
    if not restic.is_file():
        raise RuntimeError("pinned restic executable is missing")
    work = drive / f"bbs-stage8-{uuid4()}"
    try:
        work.mkdir()
        result = {
            "status": "passed",
            "drive": "D",
            "snapshot": exercise_snapshot(restic, work / "snapshot"),
            "mirror": exercise_mirror(work / "mirror-case"),
        }
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Result saved to: {RESULT_PATH}")
        return 0
    finally:
        if work.parent == drive and work.name.startswith("bbs-stage8-"):
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(
                {"status": "failed", "error": type(error).__name__, "detail": str(error)},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Stage 8 acceptance failed; see {RESULT_PATH}", file=sys.stderr)
        raise SystemExit(1) from error
