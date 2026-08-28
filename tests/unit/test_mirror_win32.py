import ctypes

import pytest

from backup_system.executor.mirror_plan import MirrorOutOfSpaceError
from backup_system.executor.mirror_win32 import MirrorIoError, WindowsMirrorFileOperations


def test_win32_errors_are_classified_without_localized_text() -> None:
    with pytest.raises(MirrorOutOfSpaceError, match="112"):
        WindowsMirrorFileOperations._raise_code("CopyFile2", 112)

    with pytest.raises(MirrorIoError, match="5"):
        WindowsMirrorFileOperations._raise_code("ReplaceFileW", 5)


def test_copyfile2_parameters_have_expected_native_size() -> None:
    from backup_system.executor.mirror_win32 import _CopyFile2ExtendedParameters

    assert ctypes.sizeof(_CopyFile2ExtendedParameters) in {20, 32}
