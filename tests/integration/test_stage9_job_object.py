import os
import subprocess
import sys
import time

import pytest

from backup_system.manager.win32_job import KillOnCloseJob


@pytest.mark.integration
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_closing_job_terminates_contained_process() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = KillOnCloseJob()
        job.assign(process.pid)
        job.close()
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
