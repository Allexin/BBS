"""Local control command surface; spool writes arrive in stage 2."""

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Sequence
from enum import IntEnum
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
from backup_system.common.config_io import (
    ConfigLoadError,
    config_validation_warnings,
    validate_config_tree,
    validate_job_with_owner,
)
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.ids import new_command_id, parse_uuid4
from backup_system.common.runtime import RuntimeRootError, discover_runtime_root
from backup_system.common.time import utc_now
from backup_system.ctl.state_reader import LocalStateReader


class QueueRemoveExitCode(IntEnum):
    REMOVED = 0
    NOT_FOUND = 2
    NOT_QUEUED = 3
    MANAGER_ERROR = 30


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
    remove.add_argument("--wait-timeout-seconds", type=float, default=30.0)
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
        "check": "check",
        "restore": "restore",
        "restore-test": "restore-test",
        "repair-mirror": "repair-mirror",
        "recover": "recover",
    }
    operation = None if arguments.command == "run" else operations.get(arguments.command)
    if arguments.command != "run" and operation is None:
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
    if arguments.command == "disk" and arguments.disk_command == "status":
        try:
            root = discover_runtime_root(Path(sys.executable))
            config = validate_job_with_owner(root / "data" / "config", arguments.job_id)
            disk = getattr(config, "disk", None)
            with LocalStateReader(root / "data" / "state" / "manager.sqlite3") as reader:
                latch = reader.disk_latch(arguments.job_id)
        except ConfigLoadError as error:
            print(str(error), file=sys.stderr)
            return ExecutorExitCode.CONFIG_INVALID
        except (OSError, RuntimeRootError, sqlite3.Error) as error:
            print(str(error), file=sys.stderr)
            return ExecutorExitCode.INTERNAL_ERROR
        projection: dict[str, object] = {
            "schema_version": 1,
            "job_id": arguments.job_id,
            "managed_disk": disk is not None,
            "state": "safety_latched"
            if latch is not None
            else ("configured" if disk is not None else "not_managed"),
            "safety_latch": latch,
        }
        if disk is not None:
            projection["mount_point"] = disk.mount_point
            projection["expected_size_bytes"] = disk.expected_size_bytes
            projection["repository_path_timeout_seconds"] = disk.repository_path_timeout_seconds
        print(json.dumps(projection, ensure_ascii=False, separators=(",", ":")))
        return 0
    if arguments.command == "config" and arguments.config_command == "validate":
        try:
            root = discover_runtime_root(Path(sys.executable))
            manager_path = root / "data" / "config" / "manager.yaml"
            manager, _ = validate_config_tree(manager_path)
            for warning in config_validation_warnings(manager_path.parent, manager):
                print(warning)
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
        if isinstance(command, QueueRemoveCommand):
            try:
                result = _wait_command_result(
                    root,
                    command.command_id,
                    timeout_seconds=arguments.wait_timeout_seconds,
                )
            except (OSError, ValueError, TimeoutError) as error:
                print(
                    json.dumps(
                        {"result": "manager_error", "error": str(error)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return QueueRemoveExitCode.MANAGER_ERROR
            disposition = str(result["disposition"])
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return {
                "removed": QueueRemoveExitCode.REMOVED,
                "not_found": QueueRemoveExitCode.NOT_FOUND,
                "not_queued": QueueRemoveExitCode.NOT_QUEUED,
            }.get(disposition, QueueRemoveExitCode.MANAGER_ERROR)
    return 0


def _wait_command_result(
    root: Path, command_id: UUID, *, timeout_seconds: float
) -> dict[str, object]:
    if timeout_seconds <= 0:
        raise ValueError("command result timeout must be positive")
    path = root / "data" / "commands" / "completed" / f"{command_id}.result.json"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "manager did not publish command result before timeout"
                ) from None
            time.sleep(0.1)
            continue
        if not isinstance(payload, dict):
            raise ValueError("manager command result must be a JSON object")
        if payload.get("schema_version") != 1 or payload.get("command_id") != str(command_id):
            raise ValueError("manager command result identity is invalid")
        if payload.get("disposition") not in {"removed", "not_found", "not_queued"}:
            raise ValueError("manager command result disposition is invalid")
        return payload
