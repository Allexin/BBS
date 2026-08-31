"""LocalSystem acceptance for atomic restic password-file ACL creation."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from backup_system.common.config import EncryptionConfig
from backup_system.executor.restic_auth import restic_auth_arguments


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args()
    result = arguments.result.resolve()
    result.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object]
    try:
        with tempfile.TemporaryDirectory(dir=result.parent) as directory:
            root = Path(directory)
            config = EncryptionConfig(mode="password", passphrase="r6-test-only-secret")
            with restic_auth_arguments(config, root) as auth:
                password_path = Path(auth[1])
                if password_path.read_text(encoding="utf-8") != "r6-test-only-secret\n":
                    raise RuntimeError("test secret read-back mismatch")
            if password_path.exists():
                raise RuntimeError("password file survived context cleanup")
        payload = {"result": "success", "identity": "LocalSystem", "secret": "test-only"}
        exit_code = 0
    except Exception as error:
        payload = {"result": "failed", "error": f"{type(error).__name__}: {error}"}
        exit_code = 1
    temporary = result.with_suffix(result.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
    temporary.replace(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
