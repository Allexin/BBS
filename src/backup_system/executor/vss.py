"""Exact VSS snapshot-set ownership and cleanup around a replaceable COM backend."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar
from uuid import UUID

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class VssSnapshot:
    snapshot_set_id: UUID
    snapshot_id: UUID
    volume_guid: str
    shadow_device_path: str


class VssBackend(Protocol):
    def create_client_accessible_snapshot(self, volume_guid: str) -> VssSnapshot: ...

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None: ...


class VssCleanupError(RuntimeError):
    def __init__(
        self,
        snapshot: VssSnapshot,
        *,
        primary_error: BaseException | None,
    ) -> None:
        super().__init__("owned VSS snapshot set could not be deleted")
        self.snapshot = snapshot
        self.primary_error = primary_error


class VssSnapshotManager:
    def __init__(self, backend: VssBackend) -> None:
        self._backend = backend

    def run(self, volume_guid: str, action: Callable[[VssSnapshot], T]) -> T:
        snapshot = self._backend.create_client_accessible_snapshot(volume_guid)
        primary_error: BaseException | None = None
        try:
            return action(snapshot)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                self._backend.delete_snapshot_set(snapshot.snapshot_set_id)
            except BaseException as cleanup_error:
                raise VssCleanupError(snapshot, primary_error=primary_error) from cleanup_error
