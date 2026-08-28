import io
import json
from datetime import UTC, datetime
from uuid import UUID

from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.executor.cancellation import CancellationRequested
from backup_system.executor.lifecycle import LifecycleCleanupError, LifecycleOperationError
from backup_system.executor.reporting import ExecutorRunReporter, JsonLineEventSink
from backup_system.executor.restic_process import ResticProcessError
from backup_system.executor.restore_target import RestoreVerificationError
from backup_system.executor.snapshot_adapter import SnapshotCursorResetWarning, SnapshotPruneWarning

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def _run(operation: object) -> tuple[list[dict[str, object]], object]:
    stream = io.StringIO()
    reporter = ExecutorRunReporter(JsonLineEventSink(stream), clock=lambda: NOW)
    outcome = reporter.execute(
        run_id=RUN_ID,
        job_id="job-1",
        operation=operation,  # type: ignore[arg-type]
    )
    return [json.loads(line) for line in stream.getvalue().splitlines()], outcome


def test_success_emits_only_json_and_exactly_one_terminal() -> None:
    events, outcome = _run(lambda: "done")
    assert [event["event"] for event in events] == [
        "run_started",
        "disk_offline_confirmed",
        "run_finished",
    ]
    assert sum(event["event"] == "run_finished" for event in events) == 1
    assert events[-1]["disk_offline_confirmed"] is True
    assert outcome.exit_code == ExecutorExitCode.SUCCESS


def test_offline_failure_has_normalized_exit_and_one_terminal() -> None:
    primary = ValueError("data failed")

    def fail() -> None:
        raise LifecycleCleanupError("offline failed", primary_error=primary)

    events, outcome = _run(fail)
    assert [event["event"] for event in events] == [
        "run_started",
        "disk_offline_failed",
        "run_finished",
    ]
    assert events[-1]["result"] == "failed"
    assert events[-1]["exit_code"] == ExecutorExitCode.DISK_OFFLINE_FAILED
    assert outcome.disk_offline_confirmed is False


def test_unexpected_error_does_not_leak_diagnostic_text_to_stdout_json() -> None:
    def fail() -> None:
        raise RuntimeError(r"secret C:\private\source")

    events, outcome = _run(fail)
    serialized = json.dumps(events)
    assert "private" not in serialized
    assert outcome.exit_code == ExecutorExitCode.INTERNAL_ERROR


def test_cooperative_cancellation_has_normalized_exit() -> None:
    def cancel() -> None:
        raise CancellationRequested("requested")

    events, outcome = _run(cancel)

    assert events[-1]["result"] == "cancelled"
    assert events[-1]["exit_code"] == ExecutorExitCode.CANCELLED
    assert outcome.exit_code == ExecutorExitCode.CANCELLED


def test_operation_failure_preserves_confirmed_offline_independently() -> None:
    def fail() -> None:
        raise LifecycleOperationError(RuntimeError("adapter failed"))

    events, outcome = _run(fail)

    assert [event["event"] for event in events[-2:]] == [
        "disk_offline_confirmed",
        "run_finished",
    ]
    assert events[-1]["result"] == "failed"
    assert events[-1]["disk_offline_confirmed"] is True
    assert outcome.exit_code == ExecutorExitCode.INTERNAL_ERROR


def test_restic_source_error_maps_to_stable_contract() -> None:
    events, outcome = _run(
        lambda: (_ for _ in ()).throw(ResticProcessError("source_read_error", "private path"))
    )
    assert events[-1]["result"] == "failed"
    assert outcome.exit_code == ExecutorExitCode.SOURCE_READ_ERROR
    assert "private" not in json.dumps(events)


def test_prune_failure_is_warning() -> None:
    events, outcome = _run(
        lambda: (_ for _ in ()).throw(SnapshotPruneWarning("maintenance failed"))
    )
    assert events[-1]["result"] == "warning"
    assert outcome.exit_code == ExecutorExitCode.SUCCESS_WITH_WARNING


def test_cursor_reset_is_warning() -> None:
    _, outcome = _run(
        lambda: (_ for _ in ()).throw(SnapshotCursorResetWarning("cursor reset"))
    )
    assert outcome.result == "warning"


def test_restore_verification_has_stable_failure_code() -> None:
    events, outcome = _run(
        lambda: (_ for _ in ()).throw(RestoreVerificationError("private restored path"))
    )
    assert outcome.exit_code == ExecutorExitCode.RESTORE_TEST_FAILED
    assert "private" not in json.dumps(events)
