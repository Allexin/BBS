from pathlib import Path

import pytest

from backup_system.manager import secrets
from backup_system.manager.secrets import ManagerSecretError, load_manager_secret


def test_secret_blob_is_bounded_decrypted_and_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "telegram-token.dpapi").write_bytes(b"protected")
    monkeypatch.setattr(secrets, "_unprotect", lambda blob: b"clear-value")
    assert load_manager_secret(tmp_path, "telegram-token") == "clear-value"


def test_secret_identifier_cannot_escape_fixed_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_manager_secret(tmp_path, "../secret")


def test_secret_rejects_multiline_clear_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "telegram-token.dpapi").write_bytes(b"protected")
    monkeypatch.setattr(secrets, "_unprotect", lambda blob: b"clear\nvalue")
    with pytest.raises(ManagerSecretError, match="value is invalid"):
        load_manager_secret(tmp_path, "telegram-token")
