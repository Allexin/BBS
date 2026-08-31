"""Snapshot adapter translating restic operations into the executor contract."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from backup_system.common.config import (
    EncryptionConfig,
    MaintenanceJobConfig,
    SnapshotJobConfig,
    SnapshotRetentionConfig,
)
from backup_system.common.excludes import restic_exclude_pattern
from backup_system.executor.restic_auth import restic_auth_arguments
from backup_system.executor.restic_process import ResticProcessError, ResticResult
from backup_system.executor.snapshot_state import LoadedSnapshotState, SnapshotStateStore


class SnapshotAdapterError(RuntimeError):
    pass


class SnapshotVerificationRequired(SnapshotAdapterError):
    pass


class SnapshotPruneWarning(SnapshotAdapterError):
    pass


class SnapshotCursorResetWarning(SnapshotAdapterError):
    pass


class ResticRunner(Protocol):
    def verify_version(self) -> tuple[int, int, int]: ...

    def run(self, arguments: Sequence[str], *, expect_json: bool = True) -> ResticResult: ...


class ResticAuthFactory(Protocol):
    def __call__(
        self, encryption: EncryptionConfig, secret_directory: Path
    ) -> AbstractContextManager[tuple[str, ...]]: ...


@dataclass(frozen=True, slots=True)
class SnapshotBackupResult:
    snapshot_id: str
    bytes_added: int
    snapshots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SnapshotCheckResult:
    mode: str
    subset_part: int | None
    subset_parts: int | None
    cursor_reset: bool


class SnapshotAdapter:
    def __init__(
        self,
        *,
        runner: ResticRunner,
        states: SnapshotStateStore,
        secret_directory: Path,
        stage_sink: Callable[[str], None] | None = None,
        snapshot_sink: Callable[[str, int], None] | None = None,
        auth_factory: ResticAuthFactory = restic_auth_arguments,
    ) -> None:
        self._runner = runner
        self._states = states
        self._secret_directory = secret_directory
        self._stage_sink = stage_sink or (lambda stage: None)
        self._snapshot_sink = snapshot_sink or (lambda snapshot_id, bytes_added: None)
        self._auth_factory = auth_factory

    def backup(self, config: SnapshotJobConfig, *, source_root: Path) -> SnapshotBackupResult:
        loaded = self._load(config)
        if loaded.state.verification_gate:
            raise SnapshotVerificationRequired("snapshot verification gate blocks backup")
        self._runner.verify_version()
        self._stage_sink("backing_up")
        with self._auth(config) as base, _exclude_file(
            config.excludes, source_root, self._secret_directory
        ) as exclude_file:
            result = self._run(
                [
                    *base,
                    "backup",
                    "--json",
                    "--use-fs-snapshot",
                    "--host",
                    config.backup.host,
                    "--tag",
                    f"job:{config.id}",
                    "--iexclude-file",
                    str(exclude_file),
                    str(source_root),
                ],
                config,
            )
            snapshot_id, bytes_added = _backup_summary(result.events)
            self._snapshot_sink(snapshot_id, bytes_added)
            if config.retention.mode == "policy":
                self._stage_sink("retention")
                retention = _retention_arguments(config.retention)
                self._run(
                    [
                        *base,
                        "forget",
                        "--json",
                        "--host",
                        config.backup.host,
                        "--tag",
                        f"job:{config.id}",
                        *retention,
                    ],
                    config,
                )
            snapshots = self._snapshot_ids(base, config)
        return SnapshotBackupResult(snapshot_id, bytes_added, snapshots)

    def check(self, config: SnapshotJobConfig, *, mode: str) -> SnapshotCheckResult:
        loaded = self._load(config)
        self._runner.verify_version()
        self._stage_sink("verifying")
        arguments = ["check", "--json"]
        part: int | None = None
        if mode == "subset":
            part = loaded.state.next_subset_part
            arguments.append(f"--read-data-subset={part}/{loaded.state.subset_parts}")
        elif mode == "full":
            arguments.append("--read-data")
        elif mode != "metadata":
            raise SnapshotAdapterError("unsupported snapshot check mode")
        try:
            with self._auth(config) as base:
                self._run([*base, *arguments], config)
        except BaseException:
            self._states.activate_gate(
                config.id, subset_parts=config.verification.data_subset_parts
            )
            raise
        self._states.complete_check(config.id, loaded.state, mode=mode)
        if loaded.cursor_reset:
            raise SnapshotCursorResetWarning("snapshot scrub cursor was reset")
        return SnapshotCheckResult(
            mode,
            part,
            loaded.state.subset_parts if mode == "subset" else None,
            loaded.cursor_reset,
        )

    def prune(self, config: MaintenanceJobConfig) -> None:
        try:
            self._runner.verify_version()
            self._stage_sink("retention")
            with self._auth_factory(config.repository.encryption, self._secret_directory) as auth:
                self._run(
                    ["--repo", config.repository.path, *auth, "prune", "--json"],
                    config,
                    expect_json=False,
                )
        except ResticProcessError as error:
            raise SnapshotPruneWarning("restic prune did not complete") from error

    def _snapshot_ids(
        self, base: Sequence[str], config: SnapshotJobConfig
    ) -> tuple[str, ...]:
        result = self._run(
            [
                *base,
                "snapshots",
                "--json",
                "--host",
                config.backup.host,
                "--tag",
                f"job:{config.id}",
            ],
            config,
        )
        ids: list[str] = []
        for event in result.events:
            value = event.get("short_id") or event.get("id")
            if isinstance(value, str):
                ids.append(value)
        return tuple(ids)

    def _load(self, config: SnapshotJobConfig) -> LoadedSnapshotState:
        return self._states.load(config.id, subset_parts=config.verification.data_subset_parts)

    @contextmanager
    def _auth(self, config: SnapshotJobConfig) -> Iterator[tuple[str, ...]]:
        with self._auth_factory(config.repository.encryption, self._secret_directory) as auth:
            yield ("--repo", config.repository.path, *auth)

    def _run(
        self,
        arguments: Sequence[str],
        config: SnapshotJobConfig | MaintenanceJobConfig,
        *,
        expect_json: bool = True,
    ) -> ResticResult:
        try:
            return self._runner.run(arguments, expect_json=expect_json)
        except ResticProcessError as error:
            if (
                error.fault == "repository_key_invalid"
                and config.repository.encryption.mode == "none"
            ):
                raise ResticProcessError(
                    "repository_auth_mode_mismatch", str(error), exit_code=error.exit_code
                ) from error
            raise


def _retention_arguments(retention: SnapshotRetentionConfig) -> tuple[str, ...]:
    return (
        "--keep-last",
        str(retention.keep_last),
        "--keep-daily",
        str(retention.keep_daily),
        "--keep-weekly",
        str(retention.keep_weekly),
        "--keep-monthly",
        str(retention.keep_monthly),
        "--keep-yearly",
        str(retention.keep_yearly),
    )


def _backup_summary(events: Sequence[Mapping[str, Any]]) -> tuple[str, int]:
    summaries = [event for event in events if event.get("message_type") == "summary"]
    if len(summaries) != 1:
        raise SnapshotAdapterError("restic backup emitted no unique summary")
    snapshot_id = summaries[0].get("snapshot_id")
    bytes_added = summaries[0].get("data_added", 0)
    if not isinstance(snapshot_id, str) or not isinstance(bytes_added, int) or bytes_added < 0:
        raise SnapshotAdapterError("restic backup summary is invalid")
    return snapshot_id, bytes_added


@contextmanager
def _exclude_file(
    excludes: Sequence[str], source_root: Path, directory: Path
) -> Iterator[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"restic-excludes-{os.getpid()}-{uuid4()}.txt"
    try:
        patterns = (restic_exclude_pattern(value, source_root) for value in excludes)
        path.write_text("\n".join(patterns) + "\n", encoding="utf-8")
        yield path
    finally:
        path.unlink(missing_ok=True)
