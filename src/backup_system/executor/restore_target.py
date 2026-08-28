"""Crash-visible, never-overwriting restore target lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.restore_request import RestoreRequest

_REPARSE_POINT = 0x400


class RestoreTargetError(RuntimeError):
    pass


class RestoreVerificationError(RestoreTargetError):
    pass


@dataclass(frozen=True, slots=True)
class RestoreManifestEntry:
    relative_path: str
    size_bytes: int
    sha256: bytes


@dataclass(frozen=True, slots=True)
class RestoreResult:
    result_path: Path
    files_restored: int
    logical_bytes: int


class RestoreTarget:
    def __init__(
        self,
        cancellation: CancellationToken,
        *,
        ready_sink: Callable[[Path], None] | None = None,
    ) -> None:
        self._cancellation = cancellation
        self._ready_sink = ready_sink or (lambda path: None)

    def create(
        self,
        request: RestoreRequest,
        *,
        forbidden_roots: Iterable[Path],
        required_bytes: int | None,
    ) -> Path:
        parent = Path(request.target)
        _validate_parent(parent, forbidden_roots)
        if required_bytes is not None and shutil.disk_usage(parent).free < required_bytes:
            raise RestoreTargetError("restore target has insufficient free space")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        result = parent / f"BackupRestore-{request.job_id}-{timestamp}-{request.request_id}"
        try:
            result.mkdir()
        except FileExistsError as error:
            raise RestoreTargetError("restore result path already exists") from error
        marker = result / ".restore-incomplete"
        try:
            with marker.open("xb") as stream:
                stream.write(
                    json.dumps(
                        {"schema_version": 1, "request_id": str(request.request_id)},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                )
                stream.flush()
                os.fsync(stream.fileno())
            _flush_directory(result)
        except BaseException:
            marker.unlink(missing_ok=True)
            result.rmdir()
            raise
        self._ready_sink(result)
        return result

    def verify_and_complete(
        self,
        result: Path,
        manifest: Iterable[RestoreManifestEntry],
    ) -> RestoreResult:
        entries = tuple(manifest)
        expected = {_path_key(item.relative_path): item for item in entries}
        actual: dict[str, Path] = {}
        for path in result.rglob("*"):
            self._cancellation.raise_if_requested()
            relative = path.relative_to(result)
            if relative.as_posix() == ".restore-incomplete":
                continue
            if path.is_symlink() or _is_reparse(path):
                raise RestoreVerificationError("restore result contains a reparse point")
            if path.is_file():
                key = _path_key(relative.as_posix())
                if key in actual:
                    raise RestoreVerificationError("restore result has a path collision")
                actual[key] = path
        if set(actual) != set(expected):
            raise RestoreVerificationError("restore result file set changed")
        for key, path in actual.items():
            item = expected[key]
            if (
                path.stat().st_size != item.size_bytes
                or _sha256(path, self._cancellation) != item.sha256
            ):
                raise RestoreVerificationError("restored file content does not match backup")
        marker = result / ".restore-incomplete"
        marker.unlink()
        _flush_directory(result)
        return RestoreResult(result, len(entries), sum(item.size_bytes for item in entries))


def _validate_parent(parent: Path, forbidden_roots: Iterable[Path]) -> None:
    if not parent.is_dir() or parent.is_symlink() or _is_reparse(parent):
        raise RestoreTargetError("restore target parent must be an ordinary existing directory")
    resolved = parent.resolve(strict=True)
    for forbidden in forbidden_roots:
        try:
            forbidden_resolved = forbidden.resolve(strict=False)
            if resolved == forbidden_resolved or forbidden_resolved in resolved.parents:
                raise RestoreTargetError("restore target is inside a forbidden data root")
        except OSError as error:
            raise RestoreTargetError("restore target identity cannot be established") from error


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _sha256(path: Path, cancellation: CancellationToken) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            cancellation.raise_if_requested()
            digest.update(block)
    return digest.digest()


def _path_key(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _flush_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
