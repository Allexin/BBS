"""Executor command-line surface; operation bodies arrive in later stages."""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, Protocol, TextIO
from uuid import UUID

from backup_system.common.config import (
    ExecutorJobConfig,
    MaintenanceJobConfig,
    MirrorJobConfig,
    SmartConfig,
    SmartTestJobConfig,
    SnapshotJobConfig,
    validate_job_id,
)
from backup_system.common.config_io import (
    ConfigLoadError,
    load_smart_config,
    validate_job_with_owner,
)
from backup_system.common.events import EventBase, SmartTestDiskFinished, StageChanged
from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.common.ids import parse_uuid4
from backup_system.common.runtime import RuntimeRootError, discover_runtime_root
from backup_system.common.time import utc_now
from backup_system.executor.cancellation import (
    CancellationProtocolError,
    CancellationToken,
    StdinCancellationMonitor,
)
from backup_system.executor.lifecycle import LifecycleOperationError
from backup_system.executor.mirror_runtime import run_mirror_operation
from backup_system.executor.operation_policy import (
    OperationNotAllowedError,
    require_operation_allowed,
)
from backup_system.executor.reporting import ExecutorRunReporter, JsonLineEventSink
from backup_system.executor.restore_runtime import (
    run_restore_operation,
    run_restore_resolution_operation,
    run_restore_test_operation,
)
from backup_system.executor.runtime import run_recovery
from backup_system.executor.smart_events import build_smart_events
from backup_system.executor.smart_preflight import (
    SmartPreflight,
    SmartPreflightObservation,
    SubprocessSmartctlBackend,
)
from backup_system.executor.smart_test import (
    SmartSelfTestError,
    SmartSelfTestResult,
    SubprocessSmartSelfTestBackend,
    run_smart_self_tests,
)
from backup_system.executor.snapshot_runtime import run_snapshot_operation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup-executor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "run",
        "prune",
        "restore-test",
        "repair-mirror",
        "recover",
        "smart-test",
    ):
        child = subparsers.add_parser(command)
        child.add_argument("--run-id", required=True, type=parse_uuid4)
        child.add_argument("--job", required=True, type=validate_job_id)
    check = subparsers.add_parser("check")
    check.add_argument("--run-id", required=True, type=parse_uuid4)
    check.add_argument("--job", required=True, type=validate_job_id)
    check.add_argument("--mode", choices=("metadata", "subset", "full"), required=True)
    for command in ("resolve-restore", "restore"):
        restore = subparsers.add_parser(command)
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
        if config is None or isinstance(config, SmartTestJobConfig):
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
    if isinstance(config, SmartTestJobConfig) and arguments.command == "smart-test":
        smart_config = load_smart_config(config_dir / "smart.yaml")

        def execute_smart_test(
            token: CancellationToken,
            smart_sink: Callable[[tuple[SmartPreflightObservation, ...]], None],
            event_sink: Callable[[EventBase], None],
        ) -> None:
            try:
                event_sink(
                    StageChanged(event="stage_changed", timestamp=utc_now(), stage="smart-test")
                )
                backend = SubprocessSmartSelfTestBackend(root / "bin" / "smartctl.exe")
                disks = (
                    backend.discover()
                    if config.target.mode == "all-system"
                    else tuple(
                        item
                        for item in smart_config.disks
                        if item.id == config.target.disk_id
                    )
                )
                def report_disk(index: int, total: int) -> None:
                    event_sink(
                        StageChanged(
                            event="stage_changed",
                            timestamp=utc_now(),
                            stage=f"smart-test-{index}-of-{total}",
                        )
                    )

                def report_result(result: SmartSelfTestResult) -> None:
                    event_sink(
                        SmartTestDiskFinished(
                            event="smart_test_disk_finished",
                            timestamp=utc_now(),
                            disk_id=result.disk.id,
                            identity_key=result.identity_key,
                            test_type=config.test_type,
                            result=result.result,
                            reason=result.reason,
                            duration_seconds=result.duration_seconds,
                            remaining_percent=result.remaining_percent,
                        )
                    )

                results = run_smart_self_tests(
                    backend=backend,
                    disks=disks,
                    test_type=config.test_type,
                    poll_seconds=config.poll_seconds,
                    timeout_seconds=config.timeout_seconds,
                    checkpoint=token.raise_if_requested,
                    on_disk=report_disk,
                    on_result=report_result,
                )
                observation_config = (
                    SmartConfig(
                        per_disk_timeout_seconds=smart_config.per_disk_timeout_seconds,
                        stale_after_hours=smart_config.stale_after_hours,
                        disks=disks,
                    )
                    if config.target.mode == "all-system"
                    else smart_config
                )
                observations = SmartPreflight(
                    SubprocessSmartctlBackend(root / "bin" / "smartctl.exe"),
                    cancellation_checkpoint=token.raise_if_requested,
                ).collect(observation_config)
                smart_sink(observations)
                failures = tuple(result for result in results if result.result != "success")
                if failures:
                    raise SmartSelfTestError(
                        f"SMART self-test failed for {len(failures)} of {len(disks)} disks"
                    )
                event_sink(
                    StageChanged(
                        event="stage_changed",
                        timestamp=utc_now(),
                        stage=(
                            "smart-test-completed"
                        ),
                    )
                )
            except BaseException as error:
                raise LifecycleOperationError(error) from error

        return _execute_operation(
            run_id=arguments.run_id,
            job_id=arguments.job,
            operation=execute_smart_test,
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout,
        )
    if isinstance(config, MirrorJobConfig) and arguments.command in {
        "run",
        "check",
        "repair-mirror",
    }:
        smart_config = load_smart_config(config_dir / "smart.yaml")
        return _execute_operation(
            run_id=arguments.run_id,
            job_id=arguments.job,
            operation=lambda token, smart_sink, event_sink: run_mirror_operation(
                runtime_root=root,
                config=config,
                smart_config=smart_config,
                run_id=arguments.run_id,
                operation=arguments.command,
                mode=getattr(arguments, "mode", None),
                cancellation=token,
                smart_sink=smart_sink,
            ),
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout,
        )
    if (
        isinstance(config, (SnapshotJobConfig, MirrorJobConfig))
        and arguments.command == "resolve-restore"
    ):
        smart_config = load_smart_config(config_dir / "smart.yaml")
        return _execute_operation(
            run_id=arguments.run_id,
            job_id=arguments.job,
            operation=lambda token, smart_sink, event_sink: run_restore_resolution_operation(
                runtime_root=root,
                config=config,
                smart_config=smart_config,
                request_file=Path(arguments.request_file),
                cancellation=token,
                smart_sink=smart_sink,
                event_sink=event_sink,
            ),
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout,
        )
    if isinstance(config, (SnapshotJobConfig, MirrorJobConfig)) and arguments.command == "restore":
        smart_config = load_smart_config(config_dir / "smart.yaml")
        return _execute_operation(
            run_id=arguments.run_id,
            job_id=arguments.job,
            operation=lambda token, smart_sink, event_sink: run_restore_operation(
                runtime_root=root,
                config=config,
                smart_config=smart_config,
                request_file=Path(arguments.request_file),
                cancellation=token,
                smart_sink=smart_sink,
                event_sink=event_sink,
            ),
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout,
        )
    if (
        isinstance(config, (SnapshotJobConfig, MirrorJobConfig))
        and arguments.command == "restore-test"
    ):
        smart_config = load_smart_config(config_dir / "smart.yaml")
        return _execute_operation(
            run_id=arguments.run_id,
            job_id=arguments.job,
            operation=lambda token, smart_sink, event_sink: run_restore_test_operation(
                runtime_root=root,
                config=config,
                smart_config=smart_config,
                cancellation=token,
                smart_sink=smart_sink,
                event_sink=event_sink,
            ),
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout,
        )
    if isinstance(config, (SnapshotJobConfig, MaintenanceJobConfig)) and (
        (isinstance(config, SnapshotJobConfig) and arguments.command in {"run", "check"})
        or (isinstance(config, MaintenanceJobConfig) and arguments.command == "prune")
    ):
        smart_config = load_smart_config(config_dir / "smart.yaml")
        return _execute_operation(
            run_id=arguments.run_id,
            job_id=arguments.job,
            operation=lambda token, smart_sink, event_sink: run_snapshot_operation(
                runtime_root=root,
                config=config,
                smart_config=smart_config,
                run_id=arguments.run_id,
                operation=arguments.command,
                mode=getattr(arguments, "mode", None),
                cancellation=token,
                smart_sink=smart_sink,
                event_sink=event_sink,
            ),
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout,
        )
    return 0


class _DataOperation(Protocol):
    def __call__(
        self,
        token: CancellationToken,
        smart_sink: Callable[[tuple[SmartPreflightObservation, ...]], None],
        event_sink: Callable[[EventBase], None],
    ) -> object: ...


def _execute_operation(
    *,
    run_id: UUID,
    job_id: str,
    operation: _DataOperation,
    input_stream: BinaryIO,
    output_stream: TextIO,
) -> int:
    token = CancellationToken()
    monitor = StdinCancellationMonitor(input_stream, token)
    monitor.start()
    sink = JsonLineEventSink(output_stream)

    def smart_sink(observations: tuple[SmartPreflightObservation, ...]) -> None:
        for event in build_smart_events(observations, timestamp=utc_now()):
            sink.emit(event)

    outcome = ExecutorRunReporter(sink, error_sink=_write_failure_diagnostic).execute(
        run_id=run_id,
        job_id=job_id,
        operation=lambda: operation(token, smart_sink, sink.emit),
    )
    monitor.wait(timeout_seconds=5)
    return int(outcome.exit_code)


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

    outcome = ExecutorRunReporter(
        JsonLineEventSink(output_stream), error_sink=_write_failure_diagnostic
    ).execute(
        run_id=run_id,
        job_id=job_id,
        operation=checked_operation,
    )
    monitor.wait(timeout_seconds=5)
    return int(outcome.exit_code)


def _write_failure_diagnostic(error: BaseException) -> None:
    chain: list[str] = []
    current: BaseException | None = error
    while current is not None and len(chain) < 6:
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    diagnostic = " <- ".join(chain)
    print(f"executor operation failed: {diagnostic[:4000]}", file=sys.stderr, flush=True)
