from pathlib import Path

from backup_system.common.config import EncryptionConfig
from backup_system.executor.restic_auth import restic_auth_arguments


def test_none_mode_uses_required_insecure_flag(tmp_path: Path) -> None:
    with restic_auth_arguments(EncryptionConfig(mode="none"), tmp_path) as arguments:
        assert arguments == ("--insecure-no-password",)
    assert list(tmp_path.glob("*")) == []


def test_password_file_is_protected_and_removed(tmp_path: Path) -> None:
    protected: list[Path] = []
    config = EncryptionConfig(mode="password", passphrase="test-only-secret")
    with restic_auth_arguments(config, tmp_path, protect=protected.append) as arguments:
        path = Path(arguments[1])
        assert arguments[0] == "--password-file"
        assert path.read_text(encoding="utf-8") == "test-only-secret\n"
        assert protected == [path]
    assert not path.exists()
