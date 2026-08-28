import os
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_adapter import (
    MirrorAdapter,
    MirrorRepairNotAllowedError,
    MirrorVerificationError,
)
from backup_system.executor.mirror_plan import MirrorOutOfSpaceError, PortableWindowsPathKeys

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
MARKER = UUID("22222222-2222-4222-8222-222222222222")


class LocalFiles:
    def __init__(self, fail_publish: bool = False, fail_delete: bool = False) -> None:
        self.fail_publish = fail_publish
        self.fail_delete = fail_delete

    def copy_to_temp(
        self,
        source: Path,
        temp: Path,
        *,
        expected_size: int,
        cancellation: CancellationToken,
    ) -> None:
        cancellation.raise_if_requested()
        temp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, temp)
        assert temp.stat().st_size == expected_size

    def publish(self, temp: Path, final: Path, *, replace_existing: bool) -> None:
        del replace_existing
        if self.fail_publish:
            self.fail_publish = False
            raise OSError("injected publish failure")
        os.replace(temp, final)

    def delete_file(self, path: Path) -> None:
        if self.fail_delete:
            self.fail_delete = False
            raise OSError("injected delete failure")
        path.unlink()

    def remove_directory(self, path: Path) -> None:
        path.rmdir()


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _adapter(files: LocalFiles | None = None) -> MirrorAdapter:
    return MirrorAdapter(
        files=files or LocalFiles(),
        cancellation=CancellationToken(),
        path_keys=PortableWindowsPathKeys(),
    )


def test_backup_produces_exact_mirror_and_full_check_finds_corruption(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    destination.mkdir()
    _write(source / "dir" / "data.bin", b"current")
    _write(source / "skip" / "secret.bin", b"secret")
    _write(destination / "old.bin", b"old")

    result = _adapter().backup(
        source_root=source,
        destination_root=destination,
        excludes=("skip",),
        job_id="job-1",
        marker_uuid=MARKER,
        run_id=RUN_ID,
        volume_free_bytes=1_000_000,
    )

    assert result.copied_files == 1
    assert result.deleted_files == 1
    assert (destination / "dir" / "data.bin").read_bytes() == b"current"
    assert not (destination / "old.bin").exists()
    assert not (destination / "skip").exists()
    checked = _adapter().check(
        destination_root=destination,
        job_id="job-1",
        marker_uuid=MARKER,
        mode="full",
    )
    assert checked.checked_bytes == 7

    (destination / "dir" / "data.bin").write_bytes(b"damage!")
    with pytest.raises(MirrorVerificationError, match="content mismatch"):
        _adapter().check(
            destination_root=destination,
            job_id="job-1",
            marker_uuid=MARKER,
            mode="full",
        )


def test_publish_failure_is_completed_by_next_run_recovery(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    destination.mkdir()
    _write(source / "data.bin", b"new")
    _write(destination / "data.bin", b"old")

    with pytest.raises(OSError, match="injected"):
        _adapter(LocalFiles(fail_publish=True)).backup(
            source_root=source,
            destination_root=destination,
            excludes=(),
            job_id="job-1",
            marker_uuid=MARKER,
            run_id=RUN_ID,
            volume_free_bytes=1_000_000,
        )

    assert (destination / "data.bin").read_bytes() == b"old"
    _adapter().backup(
        source_root=source,
        destination_root=destination,
        excludes=(),
        job_id="job-1",
        marker_uuid=MARKER,
        run_id=UUID("33333333-3333-4333-8333-333333333333"),
        volume_free_bytes=1_000_000,
    )
    assert (destination / "data.bin").read_bytes() == b"new"


def test_delete_failure_is_completed_from_tombstone(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _write(destination / "obsolete.bin", b"old")

    with pytest.raises(OSError, match="delete"):
        _adapter(LocalFiles(fail_delete=True)).backup(
            source_root=source,
            destination_root=destination,
            excludes=(),
            job_id="job-1",
            marker_uuid=MARKER,
            run_id=RUN_ID,
            volume_free_bytes=1_000_000,
        )

    assert (destination / "obsolete.bin").exists()
    _adapter().backup(
        source_root=source,
        destination_root=destination,
        excludes=(),
        job_id="job-1",
        marker_uuid=MARKER,
        run_id=UUID("33333333-3333-4333-8333-333333333333"),
        volume_free_bytes=1_000_000,
    )
    assert not (destination / "obsolete.bin").exists()


def test_out_of_space_fails_before_delete_or_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    destination.mkdir()
    _write(source / "large.bin", b"x" * 100)
    _write(destination / "old.bin", b"old")

    with pytest.raises(MirrorOutOfSpaceError):
        _adapter().backup(
            source_root=source,
            destination_root=destination,
            excludes=(),
            job_id="job-1",
            marker_uuid=MARKER,
            run_id=RUN_ID,
            volume_free_bytes=1,
        )

    assert (destination / "old.bin").read_bytes() == b"old"
    assert not (destination / "large.bin").exists()


@pytest.mark.parametrize("damage", ["missing", "unexpected", "size", "content"])
def test_full_check_detects_every_filesystem_damage(tmp_path: Path, damage: str) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    destination.mkdir()
    _write(source / "data.bin", b"valid")
    _adapter().backup(
        source_root=source,
        destination_root=destination,
        excludes=(),
        job_id="job-1",
        marker_uuid=MARKER,
        run_id=RUN_ID,
        volume_free_bytes=1_000_000,
    )

    target = destination / "data.bin"
    if damage == "missing":
        target.unlink()
    elif damage == "unexpected":
        _write(destination / "unexpected.bin", b"x")
    elif damage == "size":
        target.write_bytes(b"wrong-size")
    else:
        target.write_bytes(b"bad!!")

    with pytest.raises(MirrorVerificationError):
        _adapter().check(
            destination_root=destination,
            job_id="job-1",
            marker_uuid=MARKER,
            mode="full",
        )


def test_manual_repair_requires_gate_and_clears_it_only_after_full_check(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    destination.mkdir()
    _write(source / "data.bin", b"valid")
    adapter = _adapter()
    adapter.backup(
        source_root=source,
        destination_root=destination,
        excludes=(),
        job_id="job-1",
        marker_uuid=MARKER,
        run_id=RUN_ID,
        volume_free_bytes=1_000_000,
    )
    with pytest.raises(MirrorRepairNotAllowedError, match="active verification gate"):
        adapter.repair(
            source_root=source,
            destination_root=destination,
            excludes=(),
            job_id="job-1",
            marker_uuid=MARKER,
            run_id=UUID("33333333-3333-4333-8333-333333333333"),
            volume_free_bytes=1_000_000,
        )

    (destination / "data.bin").write_bytes(b"bad!!")
    with pytest.raises(MirrorVerificationError):
        adapter.check(
            destination_root=destination,
            job_id="job-1",
            marker_uuid=MARKER,
            mode="full",
        )
    with pytest.raises(MirrorVerificationError, match="gate"):
        adapter.backup(
            source_root=source,
            destination_root=destination,
            excludes=(),
            job_id="job-1",
            marker_uuid=MARKER,
            run_id=UUID("44444444-4444-4444-8444-444444444444"),
            volume_free_bytes=1_000_000,
        )

    repaired = adapter.repair(
        source_root=source,
        destination_root=destination,
        excludes=(),
        job_id="job-1",
        marker_uuid=MARKER,
        run_id=UUID("55555555-5555-4555-8555-555555555555"),
        volume_free_bytes=1_000_000,
    )
    assert repaired.copied_files == 1
    assert (destination / "data.bin").read_bytes() == b"valid"
