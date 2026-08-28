import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("BBS_RUN_STAGE0_INTEGRATION") != "1",
        reason="set BBS_RUN_STAGE0_INTEGRATION=1 to run the restic PoC suite",
    ),
]


@pytest.mark.timeout(300)
@pytest.mark.parametrize("script", ["restic_local.py", "restic_fail_fast.py"])
def test_stage0_restic_probe(script: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "poc" / "stage0" / script)],
        cwd=PROJECT_ROOT,
        check=False,
        timeout=290,
    )
    assert completed.returncode == 0
