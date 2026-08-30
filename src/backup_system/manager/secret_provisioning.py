"""Interactive, local-only provisioning of DPAPI manager secrets."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import uuid4

from backup_system.common.config import validate_job_id
from backup_system.deployment.security import apply_administrative_acl

Protector = Callable[[bytes], bytes]
AclApplier = Callable[[Path], None]


class SecretProvisioningError(RuntimeError):
    pass


def protect_secret(
    directory: Path,
    secret_id: str,
    value: str,
    *,
    replace: bool = False,
    protector: Protector | None = None,
    acl_applier: AclApplier = apply_administrative_acl,
) -> Path:
    validate_job_id(secret_id)
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise SecretProvisioningError("secret value must be non-empty and single-line")
    directory.mkdir(parents=True, exist_ok=True)
    acl_applier(directory)
    destination = directory / f"{secret_id}.dpapi"
    if destination.exists() and not replace:
        raise SecretProvisioningError("secret already exists; use --replace to rotate it")
    blob = (protector or _protect)(value.encode("utf-8"))
    temporary = directory / f".{secret_id}.{uuid4()}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        acl_applier(temporary)
        os.replace(temporary, destination)
        acl_applier(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _protect(clear: bytes) -> bytes:
    if os.name != "nt":
        raise SecretProvisioningError("manager DPAPI secrets require Windows")
    import win32crypt  # type: ignore[import-untyped]

    machine_scope = 0x4
    try:
        return bytes(
            win32crypt.CryptProtectData(
                clear, None, None, None, None, machine_scope
            )
        )
    except OSError as error:
        raise SecretProvisioningError("DPAPI encryption failed") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bbs-secret")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--replace", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    first = getpass.getpass(f"Enter {arguments.name}: ")
    second = getpass.getpass("Confirm: ")
    if first != second:
        print("Secret confirmation does not match", file=sys.stderr)
        return 1
    try:
        path = protect_secret(
            arguments.directory,
            arguments.name,
            first,
            replace=arguments.replace,
        )
    except (OSError, ValueError, SecretProvisioningError) as error:
        print(f"Secret provisioning failed: {error}", file=sys.stderr)
        return 1
    print(f"Protected secret saved: {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
