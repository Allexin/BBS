"""Local control command surface; spool writes arrive in stage 2."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from backup_system.common.config import validate_job_id
from backup_system.common.config_io import ConfigLoadError, validate_config_tree
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.ids import parse_uuid4
from backup_system.common.runtime import RuntimeRootError, discover_runtime_root


def _job_id(value: str) -> str:
    try:
        return validate_job_id(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _uuid4(value: str) -> str:
    try:
        parse_uuid4(value)
    except (ValueError, AttributeError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backupctl")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("cancel-current")
    config = subparsers.add_parser("config")
    config.add_subparsers(dest="config_command", required=True).add_parser("validate")
    jobs = subparsers.add_parser("jobs")
    jobs.add_subparsers(dest="jobs_command", required=True).add_parser("list")
    queue = subparsers.add_parser("queue")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    queue_commands.add_parser("list")
    remove = queue_commands.add_parser("remove")
    remove.add_argument("operation_id", type=_uuid4)
    for command in ("run", "restore-test", "repair-mirror", "recover"):
        child = subparsers.add_parser(command)
        child.add_argument("job_id", type=_job_id)
    check = subparsers.add_parser("check")
    check.add_argument("job_id", type=_job_id)
    check.add_argument("--mode", choices=("subset", "full"), required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("job_id", type=_job_id)
    restore.add_argument("--version", required=True)
    restore.add_argument("--path", required=True)
    restore.add_argument("--target", required=True)
    disk = subparsers.add_parser("disk")
    disk_status = disk.add_subparsers(dest="disk_command", required=True).add_parser("status")
    disk_status.add_argument("job_id", type=_job_id)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "config" and arguments.config_command == "validate":
        try:
            root = discover_runtime_root(Path(sys.executable))
            validate_config_tree(root / "data" / "config" / "manager.yaml")
        except (ConfigLoadError, RuntimeRootError) as error:
            print(str(error), file=sys.stderr)
            return ExecutorExitCode.CONFIG_INVALID
    return 0
