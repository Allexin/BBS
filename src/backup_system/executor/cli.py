"""Executor command-line surface; operation bodies arrive in later stages."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from backup_system.common.config import validate_job_id
from backup_system.common.config_io import (
    ConfigLoadError,
    load_smart_config,
    validate_job_with_owner,
)
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.ids import parse_uuid4
from backup_system.common.runtime import RuntimeRootError, discover_runtime_root
from backup_system.executor.operation_policy import (
    OperationNotAllowedError,
    require_operation_allowed,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup-executor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "prune", "restore-test", "repair-mirror", "recover"):
        child = subparsers.add_parser(command)
        child.add_argument("--run-id", required=True, type=parse_uuid4)
        child.add_argument("--job", required=True, type=validate_job_id)
    check = subparsers.add_parser("check")
    check.add_argument("--run-id", required=True, type=parse_uuid4)
    check.add_argument("--job", required=True, type=validate_job_id)
    check.add_argument("--mode", choices=("metadata", "sample", "full"), required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--run-id", required=True, type=parse_uuid4)
    restore.add_argument("--job", required=True, type=validate_job_id)
    restore.add_argument("--request-file", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--job", required=True, type=validate_job_id)
    subparsers.add_parser("validate-smart-config")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        root = discover_runtime_root(Path(sys.executable))
        config_dir = root / "data" / "config"
        if arguments.command == "validate-smart-config":
            load_smart_config(config_dir / "smart.yaml")
        else:
            config = validate_job_with_owner(config_dir, arguments.job)
            if arguments.command != "validate":
                require_operation_allowed(config, arguments.command)
    except (ConfigLoadError, OperationNotAllowedError, RuntimeRootError) as error:
        print(str(error), file=sys.stderr)
        return ExecutorExitCode.CONFIG_INVALID
    return 0
