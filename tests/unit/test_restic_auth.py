from pathlib import Path

import pytest

from backup_system.common.config import EncryptionConfig
from backup_system.executor.restic_auth import restic_auth_arguments


def test_none_mode_uses_required_insecure_flag(tmp_path: Path) -> None:
    with restic_auth_arguments(EncryptionConfig(mode="none"), tmp_path) as arguments:
        assert arguments == ("--insecure-no-password",)
    assert list(tmp_path.glob("*")) == []


def test_password_file_is_protected_and_removed(tmp_path: Path) -> None:
    protected: list[Path] = []

    def protect(path: Path) -> None:
        assert path.read_bytes() == b""
        protected.append(path)

    config = EncryptionConfig(mode="password", passphrase="test-only-secret")
    assert "test-only-secret" not in repr(config)
    with restic_auth_arguments(config, tmp_path, protect=protect) as arguments:
        path = Path(arguments[1])
        assert arguments[0] == "--password-file"
        assert path.read_text(encoding="utf-8") == "test-only-secret\n"
        assert protected == [path]
    assert not path.exists()


def test_protection_failure_leaves_no_secret_file(tmp_path: Path) -> None:
    config = EncryptionConfig(mode="password", passphrase="test-only-secret")

    def reject(path: Path) -> None:
        assert path.read_bytes() == b""
        raise RuntimeError("test protection failure")

    with (
        pytest.raises(RuntimeError, match="test protection failure"),
        restic_auth_arguments(config, tmp_path, protect=reject),
    ):
        raise AssertionError("unreachable")
    assert list(tmp_path.glob("*")) == []
