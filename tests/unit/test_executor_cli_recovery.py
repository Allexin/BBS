import io
import json
import time
from uuid import UUID

from backup_system.common.exit_codes import ExecutorExitCode
from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.cli import _execute_recovery

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


def _events(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_recover_cli_emits_normalized_success_envelope() -> None:
    output = io.StringIO()
    calls: list[object] = []

    code = _execute_recovery(
        run_id=RUN_ID,
        job_id="job-1",
        operation=lambda token: calls.append(token),
        input_stream=io.BytesIO(b""),
        output_stream=output,
    )

    events = _events(output)
    assert code == ExecutorExitCode.SUCCESS
    assert [event["event"] for event in events] == [
        "run_started",
        "disk_offline_confirmed",
        "run_finished",
    ]
    assert len(calls) == 1


def test_recover_cli_defers_cancel_until_cleanup_operation_finishes() -> None:
    output = io.StringIO()
    calls: list[str] = []

    def recover(token: CancellationToken) -> None:
        del token
        calls.append("recovered")
        time.sleep(0.02)

    code = _execute_recovery(
        run_id=RUN_ID,
        job_id="job-1",
        operation=recover,
        input_stream=io.BytesIO(b"cancel\n"),
        output_stream=output,
    )

    events = _events(output)
    assert calls == ["recovered"]
    assert code == ExecutorExitCode.CANCELLED
    assert events[-2]["event"] == "disk_offline_confirmed"
    assert events[-1]["result"] == "cancelled"
    assert events[-1]["disk_offline_confirmed"] is True


def test_recover_cli_rejects_unknown_stdin_frame_after_cleanup() -> None:
    output = io.StringIO()

    code = _execute_recovery(
        run_id=RUN_ID,
        job_id="job-1",
        operation=lambda token: time.sleep(0.02),
        input_stream=io.BytesIO(b"stop\n"),
        output_stream=output,
    )

    events = _events(output)
    assert code == ExecutorExitCode.INTERNAL_ERROR
    assert events[-1]["result"] == "failed"
    assert events[-1]["disk_offline_confirmed"] is True
