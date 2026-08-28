"""Durable snapshot verification gate and deterministic restic scrub cursor."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from backup_system.common.config import validate_job_id


class SnapshotStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotState:
    schema_version: int = 1
    next_subset_part: int = 1
    subset_parts: int = 4
    cycle_started_at: str | None = None
    last_full_cycle_at: str | None = None
    verification_gate: bool = False


@dataclass(frozen=True, slots=True)
class LoadedSnapshotState:
    state: SnapshotState
    cursor_reset: bool
    archived_path: Path | None = None


class SnapshotStateStore:
    def __init__(self, state_directory: Path, diagnostics_directory: Path) -> None:
        self._state_directory = state_directory
        self._diagnostics_directory = diagnostics_directory

    def load(self, job_id: str, *, subset_parts: int) -> LoadedSnapshotState:
        path = self._path(job_id)
        if not path.exists():
            state = SnapshotState(subset_parts=subset_parts)
            self.save(job_id, state)
            return LoadedSnapshotState(state, False)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = envelope["state"]
            encoded = _encode(payload)
            if envelope["sha256"] != hashlib.sha256(encoded).hexdigest():
                raise ValueError("snapshot state checksum mismatch")
            state = SnapshotState(**payload)
            if (
                state.schema_version != 1
                or state.subset_parts != subset_parts
                or not 1 <= state.next_subset_part <= subset_parts
            ):
                raise ValueError("snapshot state value is unsupported")
            return LoadedSnapshotState(state, False)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            archived = self._archive_corrupt(path, job_id)
            state = SnapshotState(subset_parts=subset_parts)
            self.save(job_id, state)
            return LoadedSnapshotState(state, True, archived)

    def save(self, job_id: str, state: SnapshotState) -> None:
        path = self._path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        encoded = _encode(payload)
        envelope = {"state": payload, "sha256": hashlib.sha256(encoded).hexdigest()}
        temporary = path.with_suffix(".json.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    envelope,
                    stream,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def activate_gate(self, job_id: str, *, subset_parts: int) -> SnapshotState:
        current = self.load(job_id, subset_parts=subset_parts).state
        updated = replace(current, verification_gate=True)
        self.save(job_id, updated)
        return updated

    def complete_check(
        self,
        job_id: str,
        current: SnapshotState,
        *,
        mode: str,
        now: datetime | None = None,
    ) -> SnapshotState:
        timestamp = (now or datetime.now(UTC)).isoformat()
        if mode == "full":
            updated = replace(current, verification_gate=False, last_full_cycle_at=timestamp)
        elif mode == "subset":
            started = current.cycle_started_at or timestamp
            if current.next_subset_part == current.subset_parts:
                updated = replace(
                    current,
                    next_subset_part=1,
                    cycle_started_at=None,
                    last_full_cycle_at=timestamp,
                )
            else:
                updated = replace(
                    current,
                    next_subset_part=current.next_subset_part + 1,
                    cycle_started_at=started,
                )
        else:
            updated = current
        self.save(job_id, updated)
        return updated

    def _path(self, job_id: str) -> Path:
        return self._state_directory / f"{validate_job_id(job_id)}.json"

    def _archive_corrupt(self, path: Path, job_id: str) -> Path:
        self._diagnostics_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        archived = self._diagnostics_directory / f"{validate_job_id(job_id)}-{stamp}.json"
        os.replace(path, archived)
        return archived


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
