"""Guarded NSSM acceptance; creates only disposable services and local temp files."""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

SERVICE_STOPPED = "SERVICE_STOPPED"
SERVICE_RUNNING = "SERVICE_RUNNING"


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, check=False, capture_output=True, text=True, shell=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr.strip()}")
    return result


def set_value(nssm: Path, service: str, name: str, *values: str) -> None:
    run([str(nssm), "set", service, name, *values])


def wait_file(path: Path, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if not path.exists():
        raise RuntimeError(f"timed out waiting for {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nssm", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not bool(ctypes.windll.shell32.IsUserAnAdmin()):
        raise RuntimeError("acceptance must run from elevated PowerShell")
    nssm = args.nssm.resolve(strict=True)
    fixture = Path(__file__).with_name("service_fixture.py").resolve(strict=True)
    work = args.output.parent / f"service-work-{uuid4()}"
    work.mkdir(parents=True)
    suffix = uuid4().hex[:10]
    cleanup_service = f"BBS-Stage9-Cleanup-{suffix}"
    invalid_service = f"BBS-Stage9-Invalid-{suffix}"
    services = (cleanup_service, invalid_service)
    result: dict[str, object] = {"schema_version": 1, "passed": False}
    try:
        cleanup = work / "cleanup.txt"
        run(
            [
                str(nssm), "install", cleanup_service, sys.executable, str(fixture),
                "cleanup", "--marker", str(cleanup), "--delay", "12",
            ]
        )
        set_value(nssm, cleanup_service, "AppStopMethodSkip", "14")
        set_value(nssm, cleanup_service, "AppKillProcessTree", "0")
        set_value(nssm, cleanup_service, "AppStopMethodConsole", "4294967295")
        run([str(nssm), "start", cleanup_service])
        wait_file(cleanup.with_suffix(".ready"))
        started = time.monotonic()
        run([str(nssm), "stop", cleanup_service])
        elapsed = time.monotonic() - started
        if cleanup.read_text(encoding="ascii").strip() != "cleanup-complete":
            raise RuntimeError("cooperative cleanup marker was not completed")
        if elapsed < 11:
            raise RuntimeError("NSSM stop returned before the long cleanup completed")

        attempts = work / "invalid-starts.txt"
        run(
            [
                str(nssm), "install", invalid_service, sys.executable, str(fixture),
                "config-invalid", "--marker", str(attempts),
            ]
        )
        set_value(nssm, invalid_service, "AppExit", "Default", "Restart")
        set_value(nssm, invalid_service, "AppExit", "40", "Exit")
        set_value(nssm, invalid_service, "AppRestartDelay", "1000")
        run([str(nssm), "start", invalid_service])
        wait_file(attempts)
        time.sleep(3)
        starts = attempts.read_text(encoding="ascii").splitlines()
        status = run([str(nssm), "status", invalid_service]).stdout.strip()
        if starts != ["start"] or status != SERVICE_STOPPED:
            raise RuntimeError("config-invalid service entered a restart loop")
        result.update(
            passed=True,
            cooperative_stop_seconds=elapsed,
            config_invalid_starts=len(starts),
            config_invalid_status=status,
        )
    finally:
        for service in services:
            run([str(nssm), "stop", service], check=False)
            run([str(nssm), "remove", service, "confirm"], check=False)
        shutil.rmtree(work, ignore_errors=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Result saved to: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
