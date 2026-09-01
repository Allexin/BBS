import json
import os
import sys
import time
from pathlib import Path

import pytest

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.restic_process import ResticProcess, ResticProcessError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "nt", reason="Windows process-group contract"),
]


@pytest.mark.parametrize(
    ("line", "fault"),
    [
        ("write: There is not enough space on the disk.", "repository_out_of_space"),
    ],
)
def test_classified_fault_cooperatively_stops_real_process(line: str, fault: str) -> None:
    script = "import sys,time; print(sys.argv[1], file=sys.stderr, flush=True); time.sleep(20)"
    runner = ResticProcess(Path(sys.executable), CancellationToken(), terminate_timeout_seconds=3)
    started = time.monotonic()
    with pytest.raises(ResticProcessError) as raised:
        runner.run(["-c", script, line], expect_json=False)
    assert raised.value.fault == fault
    assert time.monotonic() - started < 5


def test_source_errors_are_collected_until_incomplete_exit() -> None:
    event = json.dumps(
        {"message_type": "error", "during": "archival", "item": "T:\\bad.bin"}
    )
    script = "import sys; print(sys.argv[1], file=sys.stderr, flush=True); raise SystemExit(3)"
    runner = ResticProcess(Path(sys.executable), CancellationToken())

    result = runner.run(["-c", script, event], expect_json=False)

    assert result.exit_code == 3
    assert result.source_read_errors == (
        {"message_type": "error", "during": "archival", "item": "T:\\bad.bin"},
    )
