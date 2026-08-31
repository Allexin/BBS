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
    descriptor: int | None = None
    try:
        if protect is None:
            descriptor = _create_protected_secret(path)
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            protect(path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(encryption.passphrase.get_secret_value())
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        yield ("--password-file", str(path))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        path.unlink(missing_ok=True)


def _create_protected_secret(path: Path) -> int:
    if os.name != "nt":
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return _create_protected_secret_windows(path)


def _create_protected_secret_windows(path: Path) -> int:
    import msvcrt

    import pywintypes  # type: ignore[import-untyped]
    import win32api  # type: ignore[import-untyped]
    import win32con  # type: ignore[import-untyped]
    import win32file  # type: ignore[import-untyped]
    import win32security  # type: ignore[import-untyped]

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    if current_sid != system_sid:
        raise ResticSecretError("password mode requires the LocalSystem executor identity")

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        "D:P(A;;FA;;;SY)", win32security.SDDL_REVISION_1
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_WRITE,
        0,
        attributes,
        win32con.CREATE_NEW,
        win32con.FILE_ATTRIBUTE_TEMPORARY,
        None,
    )
    raw_handle: int | None = None
    try:
        actual = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
        actual_dacl = actual.GetSecurityDescriptorDacl()
        if actual_dacl is None or actual_dacl.GetAceCount() != 1:
            raise ResticSecretError("password file DACL read-back mismatch")
        ace = actual_dacl.GetAce(0)
        if ace[1] != win32con.FILE_ALL_ACCESS or ace[2] != system_sid:
            raise ResticSecretError("password file DACL read-back mismatch")
        raw_handle = handle.Detach()
        return msvcrt.open_osfhandle(raw_handle, os.O_WRONLY)
    except Exception:
        if raw_handle is not None:
            win32api.CloseHandle(raw_handle)
        else:
            handle.Close()
        raise
