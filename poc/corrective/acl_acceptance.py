"""Elevated disposable acceptance for the Stable ACL policy."""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from backup_system.deployment.security import apply_stable_acls


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("ACL acceptance requires an elevated terminal")
    work_root = arguments.work_root.resolve(strict=True)
    target = work_root / f"acl-stable-{uuid4()}"
    if target.exists():
        raise RuntimeError("unique ACL test target unexpectedly exists")
    passed = False
    try:
        (target / "data/public").mkdir(parents=True)
        (target / "data/config").mkdir()
        (target / "backup-system.root").write_text("test\n", encoding="ascii")
        apply_stable_acls(target, nginx_account=arguments.account)
        passed = True
    finally:
        if target.parent != work_root or not target.name.startswith("acl-stable-"):
            raise RuntimeError("refusing unsafe ACL test cleanup")
        shutil.rmtree(target, ignore_errors=False)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(
            {"schema_version": 1, "passed": passed, "account": arguments.account},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Result saved to: {arguments.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ACL acceptance failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
