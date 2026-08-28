"""Local control command surface; spool writes arrive in stage 2."""

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from backup_system.common.commands import (
    CancelCurrentCommand,
    CommandBase,
    QueueRemoveCommand,
    RunCommand,
    publish_command,
)
from backup_system.common.config import validate_job_id
from backup_system.common.config_io import ConfigLoadError, validate_config_tree
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.ids import new_command_id, parse_uuid4
from backup_system.common.runtime import RuntimeRootError, discover_runtime_root
from backup_system.common.time import utc_now
from backup_system.ctl.state_reader import LocalStateReader


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


def command_from_arguments(arguments: argparse.Namespace) -> CommandBase | None:
    command_id = new_command_id()
    created_at = utc_now()
    if arguments.command == "cancel-current":
        return CancelCurrentCommand(
            command_id=command_id, created_at=created_at, kind="cancel-current"
        )
    if arguments.command == "queue" and arguments.queue_command == "remove":
        return QueueRemoveCommand(
            command_id=command_id,
            created_at=created_at,
            kind="queue-remove",
            operation_id=UUID(arguments.operation_id),
        )
    operations = {
        "run": "backup",
        "check": "check",
        "restore": "restore",
        "restore-test": "restore-test",
        "repair-mirror": "repair-mirror",
        "recover": "recover",
    }
    operation = operations.get(arguments.command)
    if operation is None:
        return None
    values: dict[str, object] = {
        "command_id": command_id,
        "created_at": created_at,
        "kind": "run",
        "job_id": arguments.job_id,
        "operation": operation,
    }
    if arguments.command == "check":
        values["mode"] = arguments.mode
    elif arguments.command == "restore":
        values.update(
            version=arguments.version,
            path=arguments.path,
            target=arguments.target,
        )
    return RunCommand.model_validate(values)


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
    if arguments.command in {"status", "jobs", "queue"} and not (
        arguments.command == "queue" and arguments.queue_command == "remove"
    ):
        try:
            root = discover_runtime_root(Path(sys.executable))
            with LocalStateReader(root / "data" / "state" / "manager.sqlite3") as reader:
                if arguments.command == "status":
                    projection = reader.status()
                elif arguments.command == "jobs":
                    projection = reader.jobs()
                else:
                    projection = reader.queue()
        except (OSError, RuntimeRootError, sqlite3.Error) as error:
            print(str(error), file=sys.stderr)
            return ExecutorExitCode.INTERNAL_ERROR
        print(json.dumps(projection, ensure_ascii=False, separators=(",", ":")))
        return 0
    command = command_from_arguments(arguments)
    if command is not None:
        try:
            root = discover_runtime_root(Path(sys.executable))
            destination = publish_command(root, command)
        except (OSError, RuntimeRootError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return ExecutorExitCode.INTERNAL_ERROR
        print(f"command_id={command.command_id} path={destination}")
    return 0
