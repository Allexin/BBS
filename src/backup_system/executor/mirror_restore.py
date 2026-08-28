"""Verified restore from the durable mirror catalog."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path, PureWindowsPath
from typing import Protocol
from uuid import UUID

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_catalog import CatalogEntry, MirrorCatalog
from backup_system.executor.restore_request import RestoreRequest
from backup_system.executor.restore_target import (
    RestoreManifestEntry,
    RestoreResult,
    RestoreTarget,
    RestoreTargetError,
)


class RestoreFileCopier(Protocol):
    def __call__(self, source: Path, destination: Path, size_bytes: int) -> None: ...


class MirrorRestore:
    def __init__(
        self,
        *,
        cancellation: CancellationToken,
        copy_file: RestoreFileCopier,
        stage_sink: Callable[[str], None] | None = None,
        ready_sink: Callable[[Path], None] | None = None,
        progress_sink: Callable[[str, int, int, int, int], None] | None = None,
    ) -> None:
        self._cancellation = cancellation
        self._copy_file = copy_file
        self._stage_sink = stage_sink or (lambda stage: None)
        self._ready_sink = ready_sink
        self._progress_sink = progress_sink or (
            lambda stage, files_done, files_total, bytes_done, bytes_total: None
        )

    def run(
        self,
        *,
        destination_root: Path,
        source_root: Path,
        request: RestoreRequest,
        job_id: str,
        marker_uuid: UUID,
    ) -> RestoreResult:
        if request.version != "latest":
            raise RestoreTargetError("mirror restore supports only version latest")
        with MirrorCatalog(
            destination_root / ".backup-system" / "catalog.sqlite3",
            job_id=job_id,
            marker_uuid=marker_uuid,
        ) as catalog:
            entries = _selected_entries(catalog.entries().values(), request.path)
        manifest = tuple(
            RestoreManifestEntry(
                entry.relative_path,
                _required_size(entry),
                _required_hash(entry),
            )
            for entry in entries
        )
        target = RestoreTarget(
            self._cancellation,
            ready_sink=self._ready_sink,
            progress_sink=lambda files_done, files_total, bytes_done, bytes_total: (
                self._progress_sink(
                    "verifying", files_done, files_total, bytes_done, bytes_total
                )
            ),
        )
        result = target.create(
            request,
            forbidden_roots=[destination_root, destination_root / ".backup-system", source_root],
            required_bytes=sum(item.size_bytes for item in manifest),
        )
        self._stage_sink("restoring")
        bytes_done = 0
        for index, item in enumerate(manifest, start=1):
            self._cancellation.raise_if_requested()
            source = destination_root / Path(item.relative_path)
            final = result / Path(item.relative_path)
            final.parent.mkdir(parents=True, exist_ok=True)
            self._copy_file(source, final, item.size_bytes)
            bytes_done += item.size_bytes
            self._progress_sink(
                "restoring", index, len(manifest), bytes_done, sum(e.size_bytes for e in manifest)
            )
        self._stage_sink("verifying")
        return target.verify_and_complete(result, manifest)


def _selected_entries(
    entries: Iterable[CatalogEntry], selection: str
) -> tuple[CatalogEntry, ...]:
    selected: list[CatalogEntry] = []
    wanted = _parts(selection)
    for entry in entries:
        if entry.desired_state != "present":
            continue
        relative = _parts(entry.relative_path)
        if selection == "." or relative == wanted or relative[: len(wanted)] == wanted:
            selected.append(entry)
    if not selected:
        raise RestoreTargetError("selected mirror path does not exist")
    return tuple(sorted(selected, key=lambda item: item.path_key))


def _parts(value: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PureWindowsPath(value).parts)


def _required_size(entry: CatalogEntry) -> int:
    if entry.size_bytes is None:
        raise RestoreTargetError("mirror catalog entry has no size")
    return entry.size_bytes


def _required_hash(entry: CatalogEntry) -> bytes:
    if entry.sha256 is None or len(entry.sha256) != 32:
        raise RestoreTargetError("mirror catalog entry has no valid hash")
    return entry.sha256
