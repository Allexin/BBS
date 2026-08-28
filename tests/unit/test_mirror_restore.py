import hashlib
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_catalog import MirrorCatalog
from backup_system.executor.mirror_restore import MirrorRestore
from backup_system.executor.restore_request import RestoreRequest
from backup_system.executor.restore_target import RestoreTargetError, RestoreVerificationError

MARKER_ID = UUID("22222222-2222-4222-8222-222222222222")
REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")


def _prepare(destination: Path) -> None:
    files = {"root.txt": b"root", "Photos/2020/image.jpg": b"image"}
    with MirrorCatalog(
        destination / ".backup-system" / "catalog.sqlite3",
        job_id="mirror",
        marker_uuid=MARKER_ID,
    ) as catalog:
        for index, (relative, content) in enumerate(files.items()):
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            catalog.accept_present(
                path_key=relative.upper(),
                relative_path=relative,
                size_bytes=len(content),
                source_mtime_ns=index,
                sha256=hashlib.sha256(content).digest(),
                temp_relative_path=f"temp-{index}",
                generation_id=REQUEST_ID,
            )
            catalog.clear_temp(relative.upper())


def _request(target: Path, selection: str = ".", version: str = "latest") -> RestoreRequest:
    return RestoreRequest.model_construct(
        schema_version=1,
        request_id=REQUEST_ID,
        job_id="mirror",
        version=version,
        path=selection,
        target=str(target),
    )


def _copy(source: Path, destination: Path, size_bytes: int) -> None:
    assert source.stat().st_size == size_bytes
    with destination.open("xb") as output, source.open("rb") as input_stream:
        shutil.copyfileobj(input_stream, output)


def test_subtree_restore_preserves_logical_parent_structure(tmp_path: Path) -> None:
    destination = tmp_path / "mirror"
    target = tmp_path / "restores"
    source = tmp_path / "original"
    target.mkdir()
    source.mkdir()
    _prepare(destination)
    outcome = MirrorRestore(cancellation=CancellationToken(), copy_file=_copy).run(
        destination_root=destination,
        source_root=source,
        request=_request(target, "Photos/2020"),
        job_id="mirror",
        marker_uuid=MARKER_ID,
    )
    assert (outcome.result_path / "Photos" / "2020" / "image.jpg").read_bytes() == b"image"
    assert not (outcome.result_path / "root.txt").exists()


def test_single_file_restore_and_whole_source_layout(tmp_path: Path) -> None:
    destination = tmp_path / "mirror"
    target = tmp_path / "restores"
    source = tmp_path / "original"
    target.mkdir()
    source.mkdir()
    _prepare(destination)
    adapter = MirrorRestore(cancellation=CancellationToken(), copy_file=_copy)
    single = adapter.run(
        destination_root=destination,
        source_root=source,
        request=_request(target, "root.txt"),
        job_id="mirror",
        marker_uuid=MARKER_ID,
    )
    assert (single.result_path / "root.txt").read_bytes() == b"root"
    whole_request = _request(target).model_copy(update={"request_id": UUID(int=REQUEST_ID.int + 1)})
    whole = adapter.run(
        destination_root=destination,
        source_root=source,
        request=whole_request,
        job_id="mirror",
        marker_uuid=MARKER_ID,
    )
    assert (whole.result_path / "Photos" / "2020" / "image.jpg").is_file()


def test_copy_corruption_keeps_incomplete_result(tmp_path: Path) -> None:
    destination = tmp_path / "mirror"
    target = tmp_path / "restores"
    source = tmp_path / "original"
    target.mkdir()
    source.mkdir()
    _prepare(destination)

    def corrupt(source_path: Path, destination_path: Path, size_bytes: int) -> None:
        del source_path, size_bytes
        destination_path.write_bytes(b"bad!")

    with pytest.raises(RestoreVerificationError):
        MirrorRestore(cancellation=CancellationToken(), copy_file=corrupt).run(
            destination_root=destination,
            source_root=source,
            request=_request(target, "root.txt"),
            job_id="mirror",
            marker_uuid=MARKER_ID,
        )
    results = list(target.glob("BackupRestore-*"))
    assert len(results) == 1
    assert (results[0] / ".restore-incomplete").exists()


def test_mirror_rejects_non_latest_version(tmp_path: Path) -> None:
    with pytest.raises(RestoreTargetError, match="latest"):
        MirrorRestore(cancellation=CancellationToken(), copy_file=_copy).run(
            destination_root=tmp_path / "mirror",
            source_root=tmp_path / "source",
            request=_request(tmp_path, version="snapshot-id"),
            job_id="mirror",
            marker_uuid=MARKER_ID,
        )
