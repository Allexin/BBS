"""Bounded DPAPI-backed manager secret loading from the protected config tree."""

from __future__ import annotations

import os
from pathlib import Path

from backup_system.common.config import validate_job_id

_MAX_SECRET_BLOB_BYTES = 64 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ManagerSecretError(RuntimeError):
    pass


def load_manager_secret(secret_directory: Path, secret_id: str) -> str:
    validate_job_id(secret_id)
    path = secret_directory / f"{secret_id}.dpapi"
    if not path.is_file() or _is_reparse(path):
        raise ManagerSecretError(f"manager secret is missing: {secret_id}")
    size = path.stat().st_size
    if size <= 0 or size > _MAX_SECRET_BLOB_BYTES:
        raise ManagerSecretError(f"manager secret blob has invalid size: {secret_id}")
    clear = _unprotect(path.read_bytes())
    try:
        value = clear.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManagerSecretError(f"manager secret is not UTF-8: {secret_id}") from error
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise ManagerSecretError(f"manager secret value is invalid: {secret_id}")
    return value


def _unprotect(blob: bytes) -> bytes:
    if os.name != "nt":
        raise ManagerSecretError("manager DPAPI secrets require Windows")
    import win32crypt  # type: ignore[import-untyped]

    try:
        _, clear = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    except OSError as error:
        raise ManagerSecretError("manager secret cannot be decrypted") from error
    return bytes(clear)


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
