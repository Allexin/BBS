import hashlib
from pathlib import Path
from uuid import UUID

import pytest

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.restore_request import RestoreRequest
from backup_system.executor.restore_target import (
    RestoreManifestEntry,
    RestoreTarget,
    RestoreTargetError,
    RestoreVerificationError,
)

REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")


def _request(parent: Path) -> RestoreRequest:
    request = RestoreRequest.model_construct(
        schema_version=1,
        request_id=REQUEST_ID,
        job_id="data",
        version="latest",
        path=".",
        target=str(parent),
    )
    return request


def test_target_is_unique_marked_and_completed_only_after_readback(tmp_path: Path) -> None:
    target = RestoreTarget(CancellationToken())
    result = target.create(_request(tmp_path), forbidden_roots=[], required_bytes=1)
    assert (result / ".restore-incomplete").is_file()
    restored = result / "folder" / "file.txt"
    restored.parent.mkdir()
    restored.write_bytes(b"verified")
    outcome = target.verify_and_complete(
        result,
        [RestoreManifestEntry("folder/file.txt", 8, hashlib.sha256(b"verified").digest())],
    )
    assert outcome.logical_bytes == 8
    assert not (result / ".restore-incomplete").exists()


def test_corruption_keeps_incomplete_marker(tmp_path: Path) -> None:
    target = RestoreTarget(CancellationToken())
    result = target.create(_request(tmp_path), forbidden_roots=[], required_bytes=None)
    (result / "file.txt").write_bytes(b"corrupt")
    with pytest.raises(RestoreVerificationError):
        target.verify_and_complete(
            result,
            [RestoreManifestEntry("file.txt", 7, hashlib.sha256(b"expected").digest())],
        )
    assert (result / ".restore-incomplete").exists()


def test_forbidden_parent_is_rejected_before_result_creation(tmp_path: Path) -> None:
    forbidden = tmp_path / "repository"
    forbidden.mkdir()
    child = forbidden / "restore"
    child.mkdir()
    with pytest.raises(RestoreTargetError, match="forbidden"):
        RestoreTarget(CancellationToken()).create(
            _request(child), forbidden_roots=[forbidden], required_bytes=None
        )


def test_existing_result_is_never_reused(tmp_path: Path) -> None:
    target = RestoreTarget(CancellationToken())
    request = _request(tmp_path)
    first = target.create(request, forbidden_roots=[], required_bytes=None)
    with pytest.raises(RestoreTargetError, match="already exists"):
        target.create(request, forbidden_roots=[], required_bytes=None)
    assert (first / ".restore-incomplete").exists()


def test_verification_progress_is_bounded_and_includes_endpoints(tmp_path: Path) -> None:
    progress: list[tuple[int, int, int, int]] = []
    target = RestoreTarget(
        CancellationToken(), progress_sink=lambda *values: progress.append(values)
    )
    result = target.create(_request(tmp_path), forbidden_roots=[], required_bytes=None)
    manifest: list[RestoreManifestEntry] = []
    for index in range(205):
        name = f"file-{index:03}.txt"
        content = bytes([index % 251])
        (result / name).write_bytes(content)
        manifest.append(RestoreManifestEntry(name, 1, hashlib.sha256(content).digest()))

    outcome = target.verify_and_complete(result, manifest)

    assert [item[0] for item in progress] == [1, 100, 200, 205]
    assert progress[-1] == (205, 205, 205, 205)
    assert outcome.logical_bytes == 205
