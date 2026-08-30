"""Install the test-only SMART job configuration into a stopped Stable tree."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from backup_system.common.config_io import validate_config_tree


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stable", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--cron", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    stable = arguments.stable.resolve(strict=True)
    if not (stable / "backup-system.root").is_file():
        raise RuntimeError("Stable root marker is missing")
    if not (stable / "bin" / "smartctl.exe").is_file():
        raise RuntimeError("Pinned Stable smartctl is missing; run the one-time admin setup")
    config = stable / "data" / "config"
    manager_path = config / "manager.yaml"
    manager = _mapping(manager_path)
    jobs = manager.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("Stable manager jobs must be a list")
    job_entry = {
        "id": "test-disk-health",
        "enabled": True,
        "display_name": "Test disk SMART short test",
        "schedule": {
            "cron": arguments.cron,
            "timezone": "Europe/Samara",
            "cycle": [{"operation": "smart-test"}],
        },
    }
    manager["jobs"] = [item for item in jobs if _job_id(item) != "test-disk-health"] + [
        job_entry
    ]
    smart = {
        "schema_version": 1,
        "per_disk_timeout_seconds": 30,
        "stale_after_hours": 48,
        "disks": [
            {
                "id": "test-disk",
                "display_name": "Test disk D",
                "identity": {
                    "device": arguments.device,
                    "serial": arguments.serial,
                    "expected_size_bytes": arguments.size,
                },
            }
        ],
    }
    job = {
        "schema_version": 1,
        "id": "test-disk-health",
        "kind": "smart-test",
        "display_name": "Test disk SMART short test",
        "target": {"mode": "all-system"},
        "test_type": "short",
        "poll_seconds": 10,
        "timeout_seconds": 900,
    }

    jobs_dir = config / "jobs"
    with tempfile.TemporaryDirectory(prefix=".bbs-smart-config-", dir=config) as temporary:
        staged_config = Path(temporary) / "config"
        staged_jobs = staged_config / "jobs"
        staged_jobs.mkdir(parents=True)
        for source in jobs_dir.glob("*.yaml"):
            shutil.copy2(source, staged_jobs / source.name)
        _write_yaml_atomic(staged_config / "smart.yaml", smart)
        _write_yaml_atomic(staged_jobs / "test-disk-health.yaml", job)
        _write_yaml_atomic(staged_config / "manager.yaml", manager)
        validate_config_tree(staged_config / "manager.yaml")

    jobs_dir.mkdir(exist_ok=True)
    _write_yaml_atomic(config / "smart.yaml", smart)
    _write_yaml_atomic(jobs_dir / "test-disk-health.yaml", job)
    _write_yaml_atomic(manager_path, manager)
    print("Stable SMART test job configured and validated.")
    print(f"Cron: {arguments.cron} Europe/Samara; test type: short.")
    return 0


def _mapping(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.name} must contain a YAML mapping")
    return value


def _job_id(value: object) -> object:
    return value.get("id") if isinstance(value, dict) else None


def _write_yaml_atomic(path: Path, value: dict[str, Any]) -> None:
    text = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
