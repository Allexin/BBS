from pathlib import Path

import pytest

from backup_system.deployment.security import (
    StableAclError,
    _normalized_dacl,
    apply_stable_acls,
    stable_acl_targets,
)


def test_readback_normalization_ignores_only_auto_inherited_flag() -> None:
    expected = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    actual = "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    assert _normalized_dacl(actual) == _normalized_dacl(expected)
    assert _normalized_dacl(actual.replace(";;;BA", ";;;BU")) != _normalized_dacl(expected)


def test_readback_normalization_accepts_windows_split_inheritable_read_execute() -> None:
    expected = "D:P(A;OICI;GXGR;;;BU)"
    actual = "D:PAI(A;;0x1200a9;;;BU)(A;OICIIO;GXGR;;;BU)"
    assert _normalized_dacl(actual) == _normalized_dacl(expected)


def test_policy_protects_data_and_limits_public_reader() -> None:
    targets = {item.relative_path: item.sddl for item in stable_acl_targets("S-1-5-21-123")}
    assert ";;;BU)" in targets["."]
    assert ";;;BU)" not in targets["data"]
    assert "(A;;GX;;;S-1-5-21-123)" in targets["data"]
    assert ";;;S-1-5-21-123)" in targets["data/public"]
    assert all(value.startswith("D:P") for value in targets.values())


class _Backend:
    def __init__(self) -> None:
        self.applied: list[tuple[str, str]] = []

    def account_sid_string(self, account: str) -> str:
        assert account == "BBS-Web"
        return "S-1-5-21-123"

    def apply_and_verify(self, path: Path, sddl: str) -> None:
        self.applied.append((path.name, sddl))


def test_acl_application_uses_only_fixed_safe_targets(tmp_path: Path) -> None:
    stable = tmp_path / "Stable"
    (stable / "data/public").mkdir(parents=True)
    backend = _Backend()
    apply_stable_acls(stable, nginx_account="BBS-Web", backend=backend)
    assert [name for name, _ in backend.applied] == ["data", "public", "Stable"]


def test_acl_application_rejects_missing_public_tree(tmp_path: Path) -> None:
    stable = tmp_path / "Stable"
    (stable / "data").mkdir(parents=True)
    with pytest.raises(StableAclError, match="missing"):
        apply_stable_acls(stable, nginx_account="BBS-Web", backend=_Backend())
