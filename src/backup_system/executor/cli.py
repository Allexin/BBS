"""Executor command-line surface; operation bodies arrive in later stages."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from backup_system.common.config_io import (
    ConfigLoadError,
    load_smart_config,
    validate_job_with_owner,
)
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.runtime import RuntimeRootError, discover_runtime_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup-executor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "prune", "restore-test", "repair-mirror", "recover"):
        child = subparsers.add_parser(command)
        child.add_argument("--run-id", required=True)
        child.add_argument("--job", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--run-id", required=True)
    check.add_argument("--job", required=True)
    check.add_argument("--mode", choices=("metadata", "sample", "full"), required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--run-id", required=True)
    restore.add_argument("--job", required=True)
    restore.add_argument("--request-file", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--job", required=True)
    subparsers.add_parser("validate-smart-config")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command not in {"validate", "validate-smart-config"}:
        return 0
    try:
        root = discover_runtime_root(Path(sys.executable))
        config_dir = root / "data" / "config"
        if arguments.command == "validate":
            validate_job_with_owner(config_dir, arguments.job)
        else:
            load_smart_config(config_dir / "smart.yaml")
    except (ConfigLoadError, RuntimeRootError) as error:
        print(str(error), file=sys.stderr)
        return ExecutorExitCode.CONFIG_INVALID
    return 0
