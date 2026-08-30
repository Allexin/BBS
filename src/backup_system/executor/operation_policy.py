"""Executor-owned job-kind operation allowlist checked before hardware access."""

from __future__ import annotations

from backup_system.common.config import (
    ExecutorJobConfig,
    MaintenanceJobConfig,
    MirrorJobConfig,
    SnapshotJobConfig,
)


class OperationNotAllowedError(ValueError):
    pass


_ALLOWED: dict[type[object], frozenset[str]] = {
    SnapshotJobConfig: frozenset(
        {"run", "check", "resolve-restore", "restore", "restore-test", "recover"}
    ),
    MirrorJobConfig: frozenset(
        {
            "run",
            "check",
            "resolve-restore",
            "restore",
            "restore-test",
            "repair-mirror",
            "recover",
        }
    ),
    MaintenanceJobConfig: frozenset({"prune", "recover"}),
}


def require_operation_allowed(config: ExecutorJobConfig, operation: str) -> None:
    allowed = _ALLOWED[type(config)]
    if operation not in allowed:
        raise OperationNotAllowedError(
            f"operation {operation!r} is not allowed for job kind {config.kind!r}"
        )
