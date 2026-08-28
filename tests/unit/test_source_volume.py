from pathlib import PureWindowsPath
from uuid import UUID

import pytest

from backup_system.executor.source_volume import SourceVolumeError, SourceVolumeResolver

VOLUME_ID = UUID("11111111-1111-4111-8111-111111111111")


class _Api:
    def __init__(self) -> None:
        self.filesystem = "NTFS"
        self.root = "F:\\"
        self.name = f"\\\\?\\Volume{{{VOLUME_ID}}}\\"

    def volume_path(self, source_path: str) -> str:
        return self.root

    def volume_name(self, volume_path: str) -> str:
        return self.name

    def filesystem_name(self, volume_path: str) -> str:
        return self.filesystem


def test_resolves_relative_source_inside_canonical_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backup_system.executor.source_volume.Path.is_dir", lambda path: True)
    resolved = SourceVolumeResolver(_Api()).resolve(r"F:\Data\Current")

    assert resolved.volume_guid == VOLUME_ID
    assert resolved.volume_name == f"\\\\?\\Volume{{{VOLUME_ID}}}\\"
    assert resolved.relative_root == PureWindowsPath(r"Data\Current")
    assert resolved.shadow_root("\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy7\\") == (
        PureWindowsPath(r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy7\Data\Current")
    )


def test_volume_root_maps_to_shadow_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backup_system.executor.source_volume.Path.is_dir", lambda path: True)
    resolved = SourceVolumeResolver(_Api()).resolve("F:\\")
    assert resolved.relative_root == PureWindowsPath(".")
    assert resolved.shadow_root("shadow\\") == PureWindowsPath("shadow")


def test_rejects_non_ntfs_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backup_system.executor.source_volume.Path.is_dir", lambda path: True)
    api = _Api()
    api.filesystem = "ReFS"
    with pytest.raises(SourceVolumeError, match="NTFS"):
        SourceVolumeResolver(api).resolve("F:\\")


def test_rejects_missing_source_before_volume_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backup_system.executor.source_volume.Path.is_dir", lambda path: False)
    with pytest.raises(SourceVolumeError, match="missing"):
        SourceVolumeResolver(_Api()).resolve(r"F:\missing")


def test_rejects_source_outside_reported_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backup_system.executor.source_volume.Path.is_dir", lambda path: True)
    api = _Api()
    api.root = "G:\\"
    with pytest.raises(SourceVolumeError, match="could not be resolved"):
        SourceVolumeResolver(api).resolve(r"F:\Data")
