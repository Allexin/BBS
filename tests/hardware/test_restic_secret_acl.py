import os
from pathlib import Path

import pytest

from backup_system.common.config import EncryptionConfig
from backup_system.executor.restic_auth import restic_auth_arguments


def _running_as_local_system() -> bool:
    if os.name != "nt":
        return False
    import win32api  # type: ignore[import-untyped]
    import win32security  # type: ignore[import-untyped]

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    return bool(current_sid == system_sid)


@pytest.mark.hardware
@pytest.mark.skipif(not _running_as_local_system(), reason="requires LocalSystem identity")
def test_restic_password_file_is_created_with_verified_system_only_acl(
    tmp_path: Path,
) -> None:
    config = EncryptionConfig(mode="password", passphrase="test-only-secret")
    with restic_auth_arguments(config, tmp_path) as arguments:
        password_path = Path(arguments[1])
        assert password_path.read_text(encoding="utf-8") == "test-only-secret\n"
    assert not password_path.exists()
