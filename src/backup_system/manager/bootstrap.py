"""Fatal manager bootstrap diagnostics that do not depend on manager state."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def bootstrap_log_path(manager_config: Path) -> Path:
    """Derive the fixed Stable log path from data/config/manager.yaml."""
    config_dir = manager_config.resolve(strict=False).parent
    if config_dir.name.casefold() != "config" or config_dir.parent.name.casefold() != "data":
        raise ValueError("manager config must be located at <root>\\data\\config\\manager.yaml")
    if manager_config.name.casefold() != "manager.yaml":
        raise ValueError("manager config filename must be manager.yaml")
    return config_dir.parent / "logs" / "bootstrap.jsonl"


def write_bootstrap_failure(manager_config: Path, *, exit_code: int, diagnostic: str) -> Path:
    path = bootstrap_log_path(manager_config)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "event": "manager_bootstrap_failed",
        "timestamp": datetime.now(UTC).isoformat(),
        "exit_code": exit_code,
        "diagnostic": diagnostic,
    }
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return path
