"""Validate Stable and install/configure its NSSM service without starting it."""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
from pathlib import Path

from backup_system.deployment.nssm import configure_service

SERVICE_RUNNING = "SERVICE_RUNNING"


def output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout + "\n" + result.stderr).replace("\x00", "").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stable", type=Path, required=True)
    parser.add_argument("--nssm", type=Path, required=True)
    parser.add_argument("--service", default="BBS")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result: dict[str, object] = {"schema_version": 1, "passed": False}
    try:
        if not bool(ctypes.windll.shell32.IsUserAnAdmin()):
            raise RuntimeError("service installation requires an elevated terminal")
        stable = args.stable.resolve(strict=True)
        nssm = args.nssm.resolve(strict=True)
        status = subprocess.run(
            [str(nssm), "status", args.service],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
        if status.returncode == 0 and output(status) == SERVICE_RUNNING:
            raise RuntimeError("service must be stopped before configuration")
        python = stable / ".venv" / "Scripts" / "python.exe"
        config = stable / "data" / "config" / "manager.yaml"
        validation = subprocess.run(
            [
                str(python),
                "-m",
                "backup_system.manager",
                "--config",
                str(config),
                "--validate-only",
            ],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
        if validation.returncode != 0:
            raise RuntimeError(
                f"Stable validation failed ({validation.returncode}): "
                f"{validation.stderr.strip()}"
            )
        configure_service(nssm=nssm, service_name=args.service, root=stable)
        configured = subprocess.run(
            [str(nssm), "status", args.service],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
        result.update(
            passed=True,
            service=args.service,
            status=output(configured),
            started=False,
        )
    except Exception as error:
        result["error"] = str(error)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Result saved to: {args.output}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
