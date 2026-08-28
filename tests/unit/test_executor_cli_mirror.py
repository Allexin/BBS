import io
import json
from uuid import UUID

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.cli import _execute_operation

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_data_operation_emits_normalized_run_envelope() -> None:
    output = io.StringIO()
    calls: list[CancellationToken] = []

    code = _execute_operation(
        run_id=RUN_ID,
        job_id="job-1",
        operation=lambda token, smart_sink: calls.append(token),
        input_stream=io.BytesIO(b""),
        output_stream=output,
    )

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert code == 0
    assert [event["event"] for event in events] == [
        "run_started",
        "disk_offline_confirmed",
        "run_finished",
    ]
    assert len(calls) == 1
