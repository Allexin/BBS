"""Validated Stable service restart with fresh manager-health verification."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVICE_RUNNING = "SERVICE_RUNNING"


class RestartError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bbs-restart")
    parser.add_argument("--stable", type=Path, required=True)
    parser.add_argument("--service", default="BBS")
    parser.add_argument("--nssm", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        restart_and_verify(
            stable=arguments.stable,
            service=arguments.service,
            nssm=arguments.nssm,
            timeout_seconds=arguments.timeout_seconds,
        )
    except RestartError as error:
        print(f"BBS restart failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


def restart_and_verify(*, stable: Path, service: str, nssm: Path, timeout_seconds: int) -> None:
    if timeout_seconds <= 0:
        raise RestartError("timeout must be positive")
    root = stable.resolve(strict=True)
    if not (root / "backup-system.root").is_file():
        raise RestartError("Stable root marker is missing")
    python = root / ".venv" / "Scripts" / "python.exe"
    config = root / "data" / "config" / "manager.yaml"
    public = root / "data" / "public"
    nssm_path = nssm if nssm.is_file() else Path(shutil.which(str(nssm)) or "")
    if not python.is_file() or not config.is_file() or not nssm_path.is_file():
        raise RestartError("Stable runtime, config, or NSSM executable is missing")

    print("[1/4] Validating the complete Stable configuration.", flush=True)
    validation = _run(
        [str(python), "-m", "backup_system.manager", "--config", str(config), "--validate-only"]
    )
    if validation.returncode != 0:
        diagnostic = _bounded_output(validation)
        raise RestartError(
            f"configuration validation exited with {validation.returncode}: {diagnostic}"
        )
    if validation.stdout.strip():
        print(validation.stdout.strip(), flush=True)
    print("Configuration validation passed; service has not been changed yet.", flush=True)

    previous = _read_json(public / "health.json")
    previous_started = previous.get("manager_started_at") if previous else None
    requested_at = datetime.now(UTC)
    print(f"[2/4] Restarting Windows service {service!r}.", flush=True)
    restarted = _run([str(nssm_path), "restart", service])
    if restarted.returncode != 0:
        raise RestartError(f"NSSM restart failed: {_bounded_output(restarted)}")

    deadline = time.monotonic() + timeout_seconds
    print("[3/4] Waiting for SERVICE_RUNNING.", flush=True)
    while True:
        status = _service_status(nssm_path, service)
        if status == SERVICE_RUNNING:
            break
        if time.monotonic() >= deadline:
            raise RestartError(f"service remained {status!r}")
        print(f"Waiting for {service!r}: current status is {status}", flush=True)
        time.sleep(1)

    print("[4/4] Waiting for a fresh manager health response.", flush=True)
    while time.monotonic() < deadline:
        health = _read_json(public / "health.json")
        status_projection = _read_json(public / "status.json")
        if _is_fresh_response(
            health,
            status_projection,
            previous_started=previous_started,
            requested_at=requested_at,
        ):
            assert health is not None
            print(
                "BBS restart verified: service is running and fresh health/status "
                f"projections are available (manager_state={health['manager_state']}).",
                flush=True,
            )
            return
        current = _service_status(nssm_path, service)
        if current != SERVICE_RUNNING:
            raise RestartError(f"service left SERVICE_RUNNING and is now {current!r}")
        time.sleep(1)
    raise RestartError("service is running but no fresh manager health response was published")


def _is_fresh_response(
    health: dict[str, Any] | None,
    status: dict[str, Any] | None,
    *,
    previous_started: object,
    requested_at: datetime,
) -> bool:
    if health is None or status is None:
        return False
    if health.get("generation_id") != status.get("generation_id"):
        return False
    if health.get("manager_state") not in {"starting", "idle", "running"}:
        return False
    started_text = health.get("manager_started_at")
    generated_text = health.get("generated_at")
    if not isinstance(started_text, str) or not isinstance(generated_text, str):
        return False
    try:
        started = datetime.fromisoformat(started_text).astimezone(UTC)
        generated = datetime.fromisoformat(generated_text).astimezone(UTC)
    except ValueError:
        return False
    if previous_started is not None and started_text == previous_started:
        return False
    tolerance = timedelta(seconds=10)
    return started >= requested_at - tolerance and generated >= requested_at - tolerance


def _service_status(nssm: Path, service: str) -> str:
    result = _run([str(nssm), "status", service])
    if result.returncode != 0:
        raise RestartError(f"cannot read service status: {_bounded_output(result)}")
    return (result.stdout + result.stderr).replace("\x00", "").strip()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, shell=False)


def _bounded_output(result: subprocess.CompletedProcess[str]) -> str:
    value = (result.stdout + "\n" + result.stderr).replace("\x00", "").strip()
    return value[-4000:] or "no diagnostic output"


if __name__ == "__main__":
    raise SystemExit(main())
