"""Structured Windows Storage Management inventory for disk identity checks."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from backup_system.executor.disk_control import DiskCandidate, DiskControlError

STORAGE_NAMESPACE = r"root\Microsoft\Windows\Storage"


@dataclass(frozen=True, slots=True)
class DiskRecord:
    number: int
    serial: str
    size_bytes: int
    offline: bool
    is_boot: bool
    is_system: bool


@dataclass(frozen=True, slots=True)
class PartitionRecord:
    disk_number: int
    partition_guid: str
    access_paths: tuple[str, ...]


class StorageInventorySource(Protocol):
    def disks(self) -> Sequence[DiskRecord]: ...

    def partitions(self) -> Sequence[PartitionRecord]: ...


class ComStorageInventorySource:
    """Reads typed records from the MSFT Storage CIM provider through COM."""

    def __init__(self) -> None:
        try:
            import pythoncom  # type: ignore[import-untyped]
            import win32com.client  # type: ignore[import-untyped]
        except ImportError as error:
            raise DiskControlError("pywin32 is required for Windows storage inventory") from error
        pythoncom.CoInitialize()
        locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        self._service: Any = locator.ConnectServer(".", STORAGE_NAMESPACE)

    def disks(self) -> Sequence[DiskRecord]:
        rows: Iterable[Any] = self._service.ExecQuery(
            "SELECT Number, SerialNumber, Size, IsOffline, IsBoot, IsSystem FROM MSFT_Disk"
        )
        return tuple(
            DiskRecord(
                number=int(row.Number),
                serial=str(row.SerialNumber or "").strip(),
                size_bytes=int(row.Size),
                offline=bool(row.IsOffline),
                is_boot=bool(row.IsBoot),
                is_system=bool(row.IsSystem),
            )
            for row in rows
        )

    def partitions(self) -> Sequence[PartitionRecord]:
        rows: Iterable[Any] = self._service.ExecQuery(
            "SELECT DiskNumber, Guid, AccessPaths FROM MSFT_Partition"
        )
        return tuple(
            PartitionRecord(
                disk_number=int(row.DiskNumber),
                partition_guid=str(row.Guid or "").strip(),
                access_paths=tuple(str(path) for path in (row.AccessPaths or ())),
            )
            for row in rows
        )


class WindowsStorageInventory:
    def __init__(self, source: StorageInventorySource | None = None) -> None:
        self._source = source or ComStorageInventorySource()

    def enumerate(self) -> Sequence[DiskCandidate]:
        disks = {disk.number: disk for disk in self._source.disks()}
        candidates: list[DiskCandidate] = []
        for partition in self._source.partitions():
            disk = disks.get(partition.disk_number)
            volume_guid = _volume_guid(partition.access_paths)
            if disk is None or not partition.partition_guid or volume_guid is None:
                continue
            candidates.append(
                DiskCandidate(
                    disk_number=disk.number,
                    physical_serial=disk.serial,
                    size_bytes=disk.size_bytes,
                    partition_guid=partition.partition_guid,
                    volume_guid=volume_guid,
                    offline=disk.offline,
                    is_boot=disk.is_boot,
                    is_system=disk.is_system,
                )
            )
        return tuple(candidates)


def _volume_guid(access_paths: Sequence[str]) -> str | None:
    matches = [
        path.rstrip("\\")[len(r"\\?\Volume{") : -1]
        for path in access_paths
        if path.casefold().startswith(r"\\?\volume{") and path.rstrip("\\").endswith("}")
    ]
    if len(matches) != 1:
        return None
    return matches[0]
