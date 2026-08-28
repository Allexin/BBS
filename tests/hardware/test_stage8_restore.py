import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        os.environ.get("BBS_RUN_STAGE8_HARDWARE") != "1",
        reason="set BBS_RUN_STAGE8_HARDWARE=1 for guarded disposable-drive acceptance",
    ),
]


@pytest.mark.timeout(300)
def test_stage8_restore_acceptance_on_disposable_d() -> None:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "poc" / "stage8" / "run_restore_acceptance.ps1"),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        timeout=290,
    )
    assert completed.returncode == 0
