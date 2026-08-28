"""Durable proof of VSS snapshot-set ownership for exact recovery cleanup."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from backup_system.common.config import validate_job_id
from backup_system.executor.vss import VssSnapshot

IntentState = Literal["prepared", "created"]
T = TypeVar("T")


class VssIntentError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VssIntent:
    schema_version: int
    job_id: str
    run_id: UUID
    source_volume_guid: str
    snapshot_set_id: UUID
    snapshot_id: UUID | None
    state: IntentState


class SnapshotSetCleaner(Protocol):
    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None: ...


class PreparedVssBackend(SnapshotSetCleaner, Protocol):
    def start_snapshot_set(self) -> UUID: ...

    def complete_snapshot_set(self, snapshot_set_id: UUID, volume_guid: str) -> VssSnapshot: ...


class OwnedVssCleanupError(RuntimeError):
    def __init__(self, intent: VssIntent, primary_error: BaseException | None) -> None:
        super().__init__("owned VSS snapshot set could not be durably cleaned")
        self.intent = intent
        self.primary_error = primary_error


class VssIntentStore:
    def __init__(self, state_directory: Path) -> None:
        self._state_directory = state_directory

    def prepare(
        self,
        *,
        job_id: str,
        run_id: UUID,
        source_volume_guid: str,
        snapshot_set_id: UUID,
    ) -> VssIntent:
        validated_job_id = validate_job_id(job_id)
        existing = self.load(validated_job_id)
        if existing is not None:
            raise VssIntentError("unfinished VSS intent already exists for job")
        intent = VssIntent(
            schema_version=1,
            job_id=validated_job_id,
            run_id=run_id,
            source_volume_guid=_normalized_volume_guid(source_volume_guid),
            snapshot_set_id=snapshot_set_id,
            snapshot_id=None,
            state="prepared",
        )
        self._write(intent)
        return intent

    def mark_created(self, intent: VssIntent, snapshot_id: UUID) -> VssIntent:
        current = self._require_current(intent)
        if current.state != "prepared":
            raise VssIntentError("VSS intent is not awaiting snapshot creation")
        created = replace(current, snapshot_id=snapshot_id, state="created")
        self._write(created)
        return created

    def recover(self, job_id: str, cleaner: SnapshotSetCleaner) -> bool:
        intent = self.load(job_id)
        if intent is None:
            return False
        cleaner.delete_snapshot_set(intent.snapshot_set_id)
        self.clear(intent)
        return True

    def clear(self, intent: VssIntent) -> None:
        self._require_current(intent)
        path = self._path(intent.job_id)
        try:
            path.unlink()
        except FileNotFoundError as error:
            raise VssIntentError("VSS intent disappeared before cleanup was committed") from error
        _flush_directory(path.parent)

    def load(self, job_id: str) -> VssIntent | None:
        path = self._path(validate_job_id(job_id))
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if set(payload) != {
                "schema_version",
                "job_id",
                "run_id",
                "source_volume_guid",
                "snapshot_set_id",
                "snapshot_id",
                "state",
            }:
                raise ValueError("unexpected VSS intent fields")
            state = str(payload["state"])
            if payload["schema_version"] != 1 or state not in {"prepared", "created"}:
                raise ValueError("unsupported VSS intent")
            snapshot_id = payload["snapshot_id"]
            return VssIntent(
                schema_version=1,
                job_id=validate_job_id(str(payload["job_id"])),
                run_id=UUID(str(payload["run_id"])),
                source_volume_guid=_normalized_volume_guid(str(payload["source_volume_guid"])),
                snapshot_set_id=UUID(str(payload["snapshot_set_id"])),
                snapshot_id=UUID(str(snapshot_id)) if snapshot_id is not None else None,
                state=state,  # type: ignore[arg-type]
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise VssIntentError("VSS intent is invalid and cannot prove ownership") from error

    def _require_current(self, expected: VssIntent) -> VssIntent:
        current = self.load(expected.job_id)
        if current is None or current != expected:
            raise VssIntentError("VSS intent identity changed")
        return current

    def _write(self, intent: VssIntent) -> None:
        self._state_directory.mkdir(parents=True, exist_ok=True)
        path = self._path(intent.job_id)
        temporary = path.with_suffix(".json.tmp")
        payload = asdict(intent)
        payload["run_id"] = str(intent.run_id)
        payload["snapshot_set_id"] = str(intent.snapshot_set_id)
        payload["snapshot_id"] = str(intent.snapshot_id) if intent.snapshot_id else None
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _flush_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _path(self, job_id: str) -> Path:
        return self._state_directory / f"{validate_job_id(job_id)}.json"


class OwnedVssSnapshotManager:
    def __init__(self, backend: PreparedVssBackend, intents: VssIntentStore) -> None:
        self._backend = backend
        self._intents = intents

    def run(
        self,
        *,
        job_id: str,
        run_id: UUID,
        volume_guid: str,
        action: Callable[[VssSnapshot], T],
    ) -> T:
        snapshot_set_id = self._backend.start_snapshot_set()
        intent = self._intents.prepare(
            job_id=job_id,
            run_id=run_id,
            source_volume_guid=volume_guid,
            snapshot_set_id=snapshot_set_id,
        )
        primary_error: BaseException | None = None
        try:
            snapshot = self._backend.complete_snapshot_set(snapshot_set_id, volume_guid)
            if snapshot.snapshot_set_id != snapshot_set_id:
                raise VssIntentError("VSS backend returned a different snapshot set")
            intent = self._intents.mark_created(intent, snapshot.snapshot_id)
            return action(snapshot)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                self._backend.delete_snapshot_set(snapshot_set_id)
                self._intents.clear(intent)
            except BaseException as cleanup_error:
                raise OwnedVssCleanupError(intent, primary_error) from cleanup_error


def _normalized_volume_guid(value: str) -> str:
    stripped = value.strip().rstrip("\\")
    if stripped.casefold().startswith(r"\\?\volume{") and stripped.endswith("}"):
        stripped = stripped[len(r"\\?\Volume{") : -1]
    return str(UUID(stripped.strip("{}")))


def _flush_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
