"""Windows Stable-tree ACL policy with mandatory read-back verification."""

from __future__ import annotations

import os
import re
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
    inheritance = "OICI" if path.is_dir() else ""
    selected.apply_and_verify(
        path, f"D:P(A;{inheritance};FA;;;SY)(A;{inheritance};FA;;;BA)"
    )


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
    targets = stable_acl_targets(nginx_sid)
    by_path = {target.relative_path: target for target in targets}
    # Protect the sensitive descendants first. If root verification fails after
    # propagation, config/state still cannot inherit the broad root reader ACL.
    for relative_path in ("data", "data/public", "."):
        target = by_path[relative_path]
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
        if _normalized_dacl(actual_text) != _normalized_dacl(expected_text):
            raise StableAclError(
                f"ACL read-back mismatch for {path.name}; "
                f"expected={expected_text!r}; actual={actual_text!r}"
            )


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _normalized_dacl(value: str) -> str:
    """Normalize documented Windows canonical forms without weakening ACE semantics."""
    normalized = value.replace("D:PAI", "D:P", 1)
    # SetNamedSecurityInfo may split one inheritable read/execute ACE into an
    # object ACE plus an inherit-only child ACE. The masks below are the Windows
    # canonical rendering of GENERIC_READ | GENERIC_EXECUTE for the object.
    pattern = re.compile(
        r"\(A;;0x1200a9;;;([^)]+)\)\(A;OICIIO;GXGR;;;\1\)"
    )
    return pattern.sub(r"(A;OICI;GXGR;;;\1)", normalized)
