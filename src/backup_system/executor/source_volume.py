"""Resolve one configured source root into a verified NTFS VSS namespace."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Protocol
from uuid import UUID


class SourceVolumeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedSourceVolume:
    volume_guid: UUID
    volume_name: str
    relative_root: PureWindowsPath

    def shadow_root(self, shadow_device_path: str) -> PureWindowsPath:
        shadow = PureWindowsPath(shadow_device_path.rstrip("\\/"))
        if self.relative_root == PureWindowsPath("."):
            return shadow
        return shadow / self.relative_root


class SourceVolumeApi(Protocol):
    def volume_path(self, source_path: str) -> str: ...

    def volume_name(self, volume_path: str) -> str: ...

    def filesystem_name(self, volume_path: str) -> str: ...


class CtypesSourceVolumeApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("source volume resolution is available only on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind_signatures()

    def volume_path(self, source_path: str) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        if not self._kernel32.GetVolumePathNameW(source_path, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error(), "source volume path lookup failed")
        return str(buffer.value)

    def volume_name(self, volume_path: str) -> str:
        buffer = ctypes.create_unicode_buffer(128)
        if not self._kernel32.GetVolumeNameForVolumeMountPointW(
            _with_separator(volume_path), buffer, len(buffer)
        ):
            raise ctypes.WinError(ctypes.get_last_error(), "source volume GUID lookup failed")
        return str(buffer.value)

    def filesystem_name(self, volume_path: str) -> str:
        filesystem = ctypes.create_unicode_buffer(64)
        if not self._kernel32.GetVolumeInformationW(
            _with_separator(volume_path),
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        ):
            raise ctypes.WinError(ctypes.get_last_error(), "source filesystem lookup failed")
        return str(filesystem.value)

    def _bind_signatures(self) -> None:
        kernel32 = self._kernel32
        kernel32.GetVolumePathNameW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        kernel32.GetVolumePathNameW.restype = wintypes.BOOL
        kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        kernel32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
        kernel32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        kernel32.GetVolumeInformationW.restype = wintypes.BOOL


class SourceVolumeResolver:
    def __init__(self, api: SourceVolumeApi | None = None) -> None:
        self._api = api or CtypesSourceVolumeApi()

    def resolve(self, source_path: str) -> ResolvedSourceVolume:
        if not Path(source_path).is_dir():
            raise SourceVolumeError("configured source root is missing or is not a directory")
        try:
            volume_path = self._api.volume_path(source_path)
            volume_name = self._api.volume_name(volume_path)
            filesystem = self._api.filesystem_name(volume_path)
            volume_guid = _parse_volume_name(volume_name)
            relative_root = _relative_root(source_path, volume_path)
        except (OSError, ValueError) as error:
            raise SourceVolumeError("configured source volume could not be resolved") from error
        if filesystem.strip().casefold() != "ntfs":
            raise SourceVolumeError("configured source volume must use NTFS")
        canonical_name = f"\\\\?\\Volume{{{volume_guid}}}\\"
        return ResolvedSourceVolume(volume_guid, canonical_name, relative_root)


def _relative_root(source_path: str, volume_path: str) -> PureWindowsPath:
    source = PureWindowsPath(source_path)
    volume = PureWindowsPath(volume_path)
    source_parts = tuple(part.casefold() for part in source.parts)
    volume_parts = tuple(part.casefold() for part in volume.parts)
    if source_parts[: len(volume_parts)] != volume_parts:
        raise ValueError("source root is outside the resolved volume path")
    remaining = source.parts[len(volume.parts) :]
    return PureWindowsPath(*remaining) if remaining else PureWindowsPath(".")


def _parse_volume_name(value: str) -> UUID:
    stripped = value.strip().rstrip("\\")
    prefix = "\\\\?\\volume{"
    if not stripped.casefold().startswith(prefix) or not stripped.endswith("}"):
        raise ValueError("invalid volume GUID path")
    return UUID(stripped[len(prefix) : -1])


def _with_separator(value: str) -> str:
    return value.rstrip("\\/") + "\\"
