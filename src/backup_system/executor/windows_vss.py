"""Windows VSS backend using the structured Win32_ShadowCopy COM provider."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from backup_system.executor.vss import VssSnapshot

VSS_NAMESPACE = r"root\cimv2"
CLIENT_ACCESSIBLE = "ClientAccessible"


class VssBackendError(RuntimeError):
    def __init__(self, operation: str, code: int) -> None:
        super().__init__(f"VSS {operation} failed with code 0x{code & 0xFFFFFFFF:08x}")
        self.operation = operation
        self.code = code


class VssCreationCleanupError(RuntimeError):
    def __init__(self, snapshot_id: UUID, primary_error: BaseException) -> None:
        super().__init__("invalid created VSS snapshot could not be deleted")
        self.snapshot_id = snapshot_id
        self.primary_error = primary_error


@dataclass(frozen=True, slots=True)
class ShadowRecord:
    snapshot_id: UUID
    snapshot_set_id: UUID
    volume_name: str
    device_path: str


class VssComSource(Protocol):
    def create(self, volume_name: str, context: str) -> UUID: ...

    def get(self, snapshot_id: UUID) -> ShadowRecord | None: ...

    def in_set(self, snapshot_set_id: UUID) -> Sequence[ShadowRecord]: ...

    def delete(self, snapshot_id: UUID) -> None: ...


class WmiVssComSource:
    """Thin dynamic COM boundary; business logic only sees typed records."""

    def __init__(self) -> None:
        try:
            import pythoncom  # type: ignore[import-untyped]
            import win32com.client  # type: ignore[import-untyped]
        except ImportError as error:
            raise RuntimeError("pywin32 is required for VSS") from error
        pythoncom.CoInitialize()
        locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        self._service: Any = locator.ConnectServer(".", VSS_NAMESPACE)

    def create(self, volume_name: str, context: str) -> UUID:
        shadow_class = self._service.Get("Win32_ShadowCopy")
        method = shadow_class.Methods_("Create")
        parameters = method.InParameters.SpawnInstance_()
        parameters.Properties_.Item("Volume").Value = volume_name
        parameters.Properties_.Item("Context").Value = context
        output = self._service.ExecMethod_("Win32_ShadowCopy", "Create", parameters)
        code = int(output.Properties_.Item("ReturnValue").Value)
        if code != 0:
            raise VssBackendError("create", code)
        return UUID(str(output.Properties_.Item("ShadowID").Value).strip("{}"))

    def get(self, snapshot_id: UUID) -> ShadowRecord | None:
        rows = self._query("ID", snapshot_id)
        if not rows:
            return None
        if len(rows) != 1:
            raise VssBackendError("query snapshot", 0x80004005)
        return _record(rows[0])

    def in_set(self, snapshot_set_id: UUID) -> Sequence[ShadowRecord]:
        return tuple(_record(row) for row in self._query("SetID", snapshot_set_id))

    def delete(self, snapshot_id: UUID) -> None:
        path = f'Win32_ShadowCopy.ID="{{{snapshot_id}}}"'
        try:
            self._service.Get(path).Delete_()
        except Exception as error:
            code = int(getattr(error, "hresult", 0x80004005))
            raise VssBackendError("delete snapshot", code) from error

    def _query(self, field: str, identifier: UUID) -> list[Any]:
        query = (
            "SELECT ID, SetID, VolumeName, DeviceObject FROM Win32_ShadowCopy "
            f"WHERE {field} = '{{{identifier}}}'"
        )
        return list(self._service.ExecQuery(query))


class WindowsVssBackend:
    def __init__(self, source: VssComSource | None = None) -> None:
        self._source = source or WmiVssComSource()

    def create_client_accessible_snapshot(self, volume_guid: str) -> VssSnapshot:
        expected_volume = _volume_name(volume_guid)
        snapshot_id = self._source.create(expected_volume, CLIENT_ACCESSIBLE)
        try:
            record = self._source.get(snapshot_id)
            if record is None:
                raise VssBackendError("query created snapshot", 0x80042308)
            if _normalize_volume(record.volume_name) != _normalize_volume(expected_volume):
                raise VssBackendError("verify source volume", 0x8004230F)
            if not record.device_path.startswith(r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy"):
                raise VssBackendError("verify shadow device", 0x8004230F)
        except BaseException as primary_error:
            try:
                self._source.delete(snapshot_id)
            except BaseException as cleanup_error:
                raise VssCreationCleanupError(snapshot_id, primary_error) from cleanup_error
            raise
        return VssSnapshot(
            snapshot_set_id=record.snapshot_set_id,
            snapshot_id=record.snapshot_id,
            volume_guid=volume_guid,
            shadow_device_path=record.device_path.rstrip("\\") + "\\",
        )

    def delete_snapshot_set(self, snapshot_set_id: UUID) -> None:
        for record in self._source.in_set(snapshot_set_id):
            if record.snapshot_set_id != snapshot_set_id:
                raise VssBackendError("verify cleanup ownership", 0x8004230F)
            self._source.delete(record.snapshot_id)
        if self._source.in_set(snapshot_set_id):
            raise VssBackendError("verify snapshot deletion", 0x80042315)


def _record(row: Any) -> ShadowRecord:
    return ShadowRecord(
        snapshot_id=UUID(str(row.ID).strip("{}")),
        snapshot_set_id=UUID(str(row.SetID).strip("{}")),
        volume_name=str(row.VolumeName),
        device_path=str(row.DeviceObject),
    )


def _volume_name(value: str) -> str:
    stripped = value.strip().rstrip("\\")
    if stripped.casefold().startswith(r"\\?\volume{") and stripped.endswith("}"):
        UUID(stripped[len(r"\\?\Volume{") : -1])
        return stripped + "\\"
    identifier = UUID(stripped.strip("{}"))
    return f"\\\\?\\Volume{{{identifier}}}\\"


def _normalize_volume(value: str) -> str:
    return value.strip().rstrip("\\").casefold()
