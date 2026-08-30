"""Windows Stable-tree ACL policy with mandatory read-back verification."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class StableAclError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AclTarget:
    relative_path: str
    sddl: str


class SecurityBackend(Protocol):
    def account_sid_string(self, account: str) -> str: ...

    def apply_and_verify(self, path: Path, sddl: str) -> None: ...


def apply_administrative_acl(
    path: Path, *, backend: SecurityBackend | None = None
) -> None:
    if not path.exists() or _is_reparse(path):
        raise StableAclError("administrative ACL target is missing or unsafe")
    selected = backend or _Pywin32SecurityBackend()
    selected.apply_and_verify(path, "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")


def stable_acl_targets(nginx_sid: str) -> tuple[AclTarget, ...]:
    if not nginx_sid.startswith("S-"):
        raise StableAclError("nginx account SID is invalid")
    administrative = "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    return (
        AclTarget(".", f"D:P{administrative}(A;OICI;GRGX;;;BU)"),
        AclTarget("data", f"D:P{administrative}(A;;GX;;;{nginx_sid})"),
        AclTarget("data/public", f"D:P{administrative}(A;OICI;GRGX;;;{nginx_sid})"),
    )


def apply_stable_acls(
    stable: Path,
    *,
    nginx_account: str,
    backend: SecurityBackend | None = None,
) -> None:
    root = stable.absolute()
    if not root.is_dir() or _is_reparse(root):
        raise StableAclError("Stable root cannot be a reparse point")
    selected = backend or _Pywin32SecurityBackend()
    nginx_sid = selected.account_sid_string(nginx_account)
    for target in stable_acl_targets(nginx_sid):
        path = root if target.relative_path == "." else root / target.relative_path
        if not path.is_dir() or _is_reparse(path):
            raise StableAclError(f"ACL target is missing or unsafe: {target.relative_path}")
        selected.apply_and_verify(path, target.sddl)


class _Pywin32SecurityBackend:
    def __init__(self) -> None:
        if os.name != "nt":
            raise StableAclError("Stable ACL enforcement requires Windows")
        import win32security  # type: ignore[import-untyped]

        self._security = win32security

    def account_sid_string(self, account: str) -> str:
        if not account.strip():
            raise StableAclError("nginx account must not be empty")
        try:
            sid, _, _ = self._security.LookupAccountName(None, account)
            return str(self._security.ConvertSidToStringSid(sid))
        except OSError as error:
            raise StableAclError("nginx account could not be resolved") from error

    def apply_and_verify(self, path: Path, sddl: str) -> None:
        security = self._security
        try:
            expected = security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                sddl, security.SDDL_REVISION_1
            )
            security.SetNamedSecurityInfo(
                str(path),
                security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION
                | security.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                expected.GetSecurityDescriptorDacl(),
                None,
            )
            actual = security.GetFileSecurity(str(path), security.DACL_SECURITY_INFORMATION)
            expected_text = security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                expected, security.SDDL_REVISION_1, security.DACL_SECURITY_INFORMATION
            )
            actual_text = security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                actual, security.SDDL_REVISION_1, security.DACL_SECURITY_INFORMATION
            )
        except OSError as error:
            raise StableAclError(f"cannot apply ACL to {path.name}") from error
        if actual_text != expected_text:
            raise StableAclError(
                f"ACL read-back mismatch for {path.name}; "
                f"expected={expected_text!r}; actual={actual_text!r}"
            )


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
