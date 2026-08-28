"""Ephemeral restic authentication arguments without environment or command-line secrets."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from backup_system.common.config import EncryptionConfig


class ResticSecretError(RuntimeError):
    pass


@contextmanager
def restic_auth_arguments(
    encryption: EncryptionConfig,
    secret_directory: Path,
    *,
    protect: Callable[[Path], None] | None = None,
) -> Iterator[tuple[str, ...]]:
    if encryption.mode == "none":
        yield ("--insecure-no-password",)
        return
    if encryption.passphrase is None:
        raise ResticSecretError("password mode has no passphrase")
    secret_directory.mkdir(parents=True, exist_ok=True)
    path = secret_directory / f"restic-password-{uuid4()}.tmp"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encryption.passphrase)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        (protect or _protect_for_local_system)(path)
        yield ("--password-file", str(path))
    finally:
        try:
            path.write_bytes(b"")
        finally:
            path.unlink(missing_ok=True)


def _protect_for_local_system(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    import win32api  # type: ignore[import-untyped]
    import win32security  # type: ignore[import-untyped]

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    if not win32security.EqualSid(current_sid, system_sid):
        raise ResticSecretError("password mode requires the LocalSystem executor identity")

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        "D:P(A;;FA;;;SY)", win32security.SDDL_REVISION_1
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
