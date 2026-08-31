import pytest

from backup_system.executor.restore_runtime import _relative_control_path
from backup_system.executor.restore_target import RestoreTargetError


def test_restore_test_control_path_becomes_source_relative() -> None:
    assert _relative_control_path(r"F:\Data", r"f:\data\Control\file.bin") == (r"Control\file.bin")


def test_restore_test_control_path_cannot_escape_source() -> None:
    with pytest.raises(RestoreTargetError, match="outside"):
        _relative_control_path(r"F:\Data", r"F:\Other\file.bin")
