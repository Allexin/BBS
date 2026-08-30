"""Apply and verify the Stable ACL policy as one explicit administrative step."""

from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path

from backup_system.deployment.security import apply_stable_acls


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stable", type=Path, required=True)
    parser.add_argument("--nginx-account", required=True)
    parser.add_argument("--deployment-account", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result: dict[str, object] = {"schema_version": 1, "passed": False}
    try:
        if not bool(ctypes.windll.shell32.IsUserAnAdmin()):
            raise RuntimeError("ACL application requires an elevated terminal")
        stable = args.stable.resolve(strict=True)
        apply_stable_acls(
            stable,
            nginx_account=args.nginx_account,
            deployment_account=args.deployment_account,
        )
        result.update(passed=True, stable=str(stable))
    except Exception as error:
        result["error"] = str(error)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Result saved to: {args.output}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
