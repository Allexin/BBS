"""Non-privileged restic PoC on synthetic data.

This probe never uses production data or a physical backup disk. Its complete
workspace is recreated below the repository's ignored .poc-work directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = Path(__file__).with_name("restic.lock.json")
WORK_ROOT = PROJECT_ROOT / ".poc-work" / "stage0" / "restic-local"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_lock() -> dict[str, str]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def find_restic(lock: dict[str, str]) -> Path:
    configured = os.environ.get("BBS_RESTIC_EXE")
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(
        PROJECT_ROOT
        / ".tools"
        / f"restic-{lock['version']}"
        / f"restic_{lock['version']}_windows_amd64.exe"
    )
    located = shutil.which("restic")
    if located:
        candidates.append(Path(located))

    for candidate in candidates:
        if candidate.is_file():
            actual_hash = sha256(candidate)
            if actual_hash != lock["executable_sha256"]:
                raise RuntimeError(
                    f"restic executable hash mismatch: {candidate} ({actual_hash})"
                )
            return candidate.resolve()
    raise RuntimeError(
        "Pinned restic executable not found. See poc/stage0/README.md."
    )


def run(restic: Path, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = [
        str(restic),
        "--repo",
        str(repository),
        "--insecure-no-password",
        "--no-cache",
        *arguments,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"restic failed ({completed.returncode}): {' '.join(arguments)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def create_source(source: Path) -> dict[Path, str]:
    files: dict[Path, bytes] = {
        Path("plain.txt"): b"BBS stage zero\n",
        Path("кириллица") / "данные.txt": "Проверка резервной копии\n".encode(),
        Path("emoji") / "backup-😀.txt": "Unicode works\n".encode(),
        Path("open-file.txt"): b"This file remains open during backup.\n",
    }
    long_relative = (
        Path("long-path")
        / ("segment-a-" + "a" * 54)
        / ("segment-b-" + "b" * 54)
        / ("segment-c-" + "c" * 54)
        / ("segment-d-" + "d" * 54)
        / "long-file.txt"
    )
    files[long_relative] = b"Path longer than 260 characters.\n"

    hashes: dict[Path, str] = {}
    for relative, content in files.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        hashes[relative] = hashlib.sha256(content).hexdigest()
    if len(str(source / long_relative)) <= 260:
        raise RuntimeError("long-path fixture did not exceed 260 characters")
    return hashes


def find_restored_source(restore_root: Path, expected: dict[Path, str]) -> Path:
    for candidate in restore_root.rglob("source"):
        if not candidate.is_dir():
            continue
        if all((candidate / relative).is_file() for relative in expected):
            return candidate
    raise RuntimeError("restored source tree was not found")


def verify_files(root: Path, expected: dict[Path, str]) -> None:
    for relative, expected_hash in expected.items():
        restored = root / relative
        actual_hash = sha256(restored)
        if actual_hash != expected_hash:
            raise RuntimeError(f"restored hash mismatch: {relative}")


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("this PoC is intentionally Windows-only")
    lock = load_lock()
    restic = find_restic(lock)

    expected_root = PROJECT_ROOT / ".poc-work"
    if WORK_ROOT.parent.parent != expected_root:
        raise RuntimeError("unsafe PoC workspace path")
    if WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)

    source = WORK_ROOT / "source"
    repository = WORK_ROOT / "repository"
    restore_root = WORK_ROOT / "restore"
    source.mkdir(parents=True)
    expected = create_source(source)

    version = subprocess.run(
        [str(restic), "version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    run(restic, repository, "init", "--repository-version", "stable")

    open_path = source / "open-file.txt"
    with open_path.open("a", encoding="utf-8") as open_file:
        open_file.write("Handle is open while restic reads this file.\n")
        open_file.flush()
        expected[Path("open-file.txt")] = sha256(open_path)
        backup = run(restic, repository, "backup", "--json", str(source))

    run(restic, repository, "check", "--read-data")
    snapshots_text = run(restic, repository, "snapshots", "--json").stdout
    snapshots: list[dict[str, Any]] = json.loads(snapshots_text)
    if len(snapshots) != 1:
        raise RuntimeError(f"expected one snapshot, got {len(snapshots)}")
    run(restic, repository, "restore", "--verify", "latest", "--target", str(restore_root))

    restored_source = find_restored_source(restore_root, expected)
    verify_files(restored_source, expected)

    backup_events = [json.loads(line) for line in backup.stdout.splitlines() if line]
    summary = next(
        (event for event in reversed(backup_events) if event.get("message_type") == "summary"),
        None,
    )
    result = {
        "status": "passed",
        "restic": version,
        "snapshot_id": snapshots[0]["short_id"],
        "files_verified": len(expected),
        "long_path_length": max(len(str(source / relative)) for relative in expected),
        "backup_summary_present": summary is not None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"PoC failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
