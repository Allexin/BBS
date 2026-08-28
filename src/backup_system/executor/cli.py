"""Executor command-line surface; operation bodies arrive in later stages."""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, TextIO
from uuid import UUID

from backup_system.common.config import ExecutorJobConfig, validate_job_id
from backup_system.common.config_io import (
    ConfigLoadError,
    load_smart_config,
    validate_job_with_owner,
)
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.ids import parse_uuid4
from backup_system.common.runtime import RuntimeRootError, discover_runtime_root
from backup_system.executor.cancellation import (
    CancellationProtocolError,
    CancellationToken,
    StdinCancellationMonitor,
)
from backup_system.executor.lifecycle import LifecycleOperationError
from backup_system.executor.operation_policy import (
    OperationNotAllowedError,
    require_operation_allowed,
)
from backup_system.executor.reporting import ExecutorRunReporter, JsonLineEventSink
from backup_system.executor.runtime import run_recovery


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
    config: ExecutorJobConfig | None = None
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
    if arguments.command == "recover":
        if config is None:
            raise RuntimeError("recover config was not loaded")
        return _execute_recovery(
            run_id=arguments.run_id,
            job_id=arguments.job,
            operation=lambda token: run_recovery(
                runtime_root=root,
                config=config,
                cancellation=token,
            ),
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout,
        )
    return 0


def _execute_recovery(
    *,
    run_id: UUID,
    job_id: str,
    operation: Callable[[CancellationToken], object],
    input_stream: BinaryIO,
    output_stream: TextIO,
) -> int:
    token = CancellationToken()
    monitor = StdinCancellationMonitor(input_stream, token)
    monitor.start()

    def checkpoint() -> None:
        if (error := monitor.protocol_error) is not None:
            raise CancellationProtocolError(str(error))
        token.raise_if_requested()

    def checked_operation() -> object:
        value = operation(token)
        try:
            checkpoint()
        except BaseException as error:
            raise LifecycleOperationError(error) from error
        return value

    outcome = ExecutorRunReporter(JsonLineEventSink(output_stream)).execute(
        run_id=run_id,
        job_id=job_id,
        operation=checked_operation,
    )
    return int(outcome.exit_code)
