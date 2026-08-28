"""Verified extraction of a restic snapshot into the common restore layout."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any

from backup_system.common.config import SnapshotJobConfig
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.restic_auth import restic_auth_arguments
from backup_system.executor.restore_request import RestoreRequest
from backup_system.executor.restore_target import (
    RestoreManifestEntry,
    RestoreResult,
    RestoreTarget,
    RestoreTargetError,
)
from backup_system.executor.snapshot_adapter import ResticAuthFactory, ResticRunner

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{8,64}$")


class SnapshotRestore:
    def __init__(
        self,
        *,
        runner: ResticRunner,
        cancellation: CancellationToken,
        secret_directory: Path,
        stage_sink: Callable[[str], None] | None = None,
        auth_factory: ResticAuthFactory = restic_auth_arguments,
    ) -> None:
        self._runner = runner
        self._cancellation = cancellation
        self._secret_directory = secret_directory
        self._stage_sink = stage_sink or (lambda stage: None)
        self._auth_factory = auth_factory

    def run(self, config: SnapshotJobConfig, request: RestoreRequest) -> RestoreResult:
        self._runner.verify_version()
        target = RestoreTarget(self._cancellation)
        result = target.create(
            request,
            forbidden_roots=[Path(config.repository.path), Path(config.source.path)],
            required_bytes=None,
        )
        staging = result / ".restic-staging"
        staging.mkdir()
        with self._auth_factory(config.repository.encryption, self._secret_directory) as auth:
            base = ("--repo", config.repository.path, *auth)
            snapshot_id = self._resolve_snapshot(base, config, request.version)
            self._stage_sink("restoring")
            self._runner.run(
                [
                    *base,
                    "restore",
                    "--json",
                    "--verify",
                    "--overwrite",
                    "never",
                    snapshot_id,
                    "--target",
                    str(staging),
                ],
                expect_json=False,
            )
        source = _locate_source(staging, config.source.path)
        selected = source if request.path == "." else source / Path(request.path)
        if not selected.exists() or selected.is_symlink():
            raise RestoreTargetError("selected snapshot path does not exist")
        manifest = _publish_selected(
            source=source,
            selected=selected,
            selection=request.path,
            result=result,
            cancellation=self._cancellation,
        )
        shutil.rmtree(staging)
        self._stage_sink("verifying")
        return target.verify_and_complete(result, manifest)

    def _resolve_snapshot(
        self,
        base: Sequence[str],
        config: SnapshotJobConfig,
        requested: str,
    ) -> str:
        result = self._runner.run(
            [
                *base,
                "snapshots",
                "--json",
                "--host",
                config.backup.host,
                "--tag",
                f"job:{config.id}",
            ]
        )
        snapshots = tuple(result.events)
        if not snapshots:
            raise RestoreTargetError("snapshot repository has no matching snapshots")
        if requested == "latest":
            latest = max(snapshots, key=lambda item: str(item.get("time", "")))
            return _full_snapshot_id(latest)
        if _SNAPSHOT_ID.fullmatch(requested) is None:
            raise RestoreTargetError("snapshot ID has invalid syntax")
        matches = [
            item
            for item in snapshots
            if requested in {item.get("id"), item.get("short_id")}
        ]
        if len(matches) != 1:
            raise RestoreTargetError("snapshot ID does not uniquely belong to this job")
        return _full_snapshot_id(matches[0])


def _full_snapshot_id(snapshot: Mapping[str, Any]) -> str:
    value = snapshot.get("id")
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RestoreTargetError("restic snapshot identity is invalid")
    return value


def _locate_source(staging: Path, configured_source: str) -> Path:
    source = PureWindowsPath(configured_source)
    name = source.name or source.drive.rstrip(":")
    candidates = [path for path in staging.rglob("*") if path.is_dir() and path.name == name]
    if len(candidates) != 1:
        raise RestoreTargetError("restored source root could not be identified uniquely")
    return candidates[0]


def _publish_selected(
    *,
    source: Path,
    selected: Path,
    selection: str,
    result: Path,
    cancellation: CancellationToken,
) -> tuple[RestoreManifestEntry, ...]:
    files = (
        [selected]
        if selected.is_file()
        else [path for path in selected.rglob("*") if path.is_file()]
    )
    manifest: list[RestoreManifestEntry] = []
    for path in files:
        cancellation.raise_if_requested()
        relative = path.relative_to(source)
        final = result / relative
        final.parent.mkdir(parents=True, exist_ok=True)
        digest = _sha256(path, cancellation)
        size = path.stat().st_size
        os.replace(path, final)
        manifest.append(RestoreManifestEntry(relative.as_posix(), size, digest))
    if not manifest and selection != ".":
        raise RestoreTargetError("selected snapshot subtree contains no files")
    return tuple(manifest)


def _sha256(path: Path, cancellation: CancellationToken) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            cancellation.raise_if_requested()
            digest.update(block)
    return digest.digest()
