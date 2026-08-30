from pathlib import Path

import pytest

from backup_system.manager.secret_provisioning import (
    SecretProvisioningError,
    protect_secret,
)


def test_secret_is_atomically_protected_without_cleartext(tmp_path: Path) -> None:
    protected: list[Path] = []
    result = protect_secret(
        tmp_path,
        "telegram-token",
        "clear-value",
        protector=lambda clear: b"DPAPI:" + clear[::-1],
        acl_applier=protected.append,
    )
    assert result.read_bytes() == b"DPAPI:eulav-raelc"
    assert b"clear-value" not in result.read_bytes()
    assert protected[0] == tmp_path
    assert protected[-1] == result
    assert not tuple(tmp_path.glob("*.tmp"))


def test_existing_secret_requires_explicit_rotation(tmp_path: Path) -> None:
    target = tmp_path / "telegram-token.dpapi"
    target.write_bytes(b"existing")
    with pytest.raises(SecretProvisioningError, match="already exists"):
        protect_secret(
            tmp_path,
            "telegram-token",
            "new-value",
            protector=lambda clear: clear,
            acl_applier=lambda path: None,
        )
