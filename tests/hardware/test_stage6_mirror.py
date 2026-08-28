import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.mirror_adapter import MirrorAdapter, MirrorVerificationError
from backup_system.executor.mirror_win32 import WindowsMirrorFileOperations

PROJECT_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        os.environ.get("BBS_RUN_STAGE6_HARDWARE") != "1",
        reason="set BBS_RUN_STAGE6_HARDWARE=1 for the guarded mirror suite",
    ),
]


def _guarded_drive() -> str:
    drive = os.environ.get("BBS_HARDWARE_TEST_DRIVE", "").strip().upper()
    guard = os.environ.get("BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID", "").strip()
    if drive != "D":
        pytest.fail("stage-6 hardware acceptance is restricted to disposable drive D")
    if not guard:
        pytest.fail("hardware disk identity guard is missing")
    return drive


@pytest.mark.timeout(180)
def test_real_win32_mirror_copy_replace_and_verification() -> None:
    drive = _guarded_drive()
    token = uuid4().hex
    source = PROJECT_ROOT / ".poc-work" / "stage6" / f"source-{token}"
    destination = Path(f"{drive}:\\bbs-stage6-mirror-{token}")
    assert not destination.exists()
    try:
        source.mkdir(parents=True)
        destination.mkdir()
        (source / "data.bin").write_bytes(b"first-version")
        adapter = MirrorAdapter(
            files=WindowsMirrorFileOperations(),
            cancellation=CancellationToken(),
        )
        marker = uuid4()
        adapter.backup(
            source_root=source,
            destination_root=destination,
            excludes=(),
            job_id="stage6-hardware",
            marker_uuid=marker,
            run_id=uuid4(),
            volume_free_bytes=shutil.disk_usage(destination).free,
        )
        (source / "data.bin").write_bytes(b"second-version-is-longer")
        adapter.backup(
            source_root=source,
            destination_root=destination,
            excludes=(),
            job_id="stage6-hardware",
            marker_uuid=marker,
            run_id=uuid4(),
            volume_free_bytes=shutil.disk_usage(destination).free,
        )
        assert (destination / "data.bin").read_bytes() == b"second-version-is-longer"
        adapter.check(
            destination_root=destination,
            job_id="stage6-hardware",
            marker_uuid=marker,
            mode="full",
        )
        (destination / "data.bin").write_bytes(b"damaged-version-is-longer")
        with pytest.raises(MirrorVerificationError):
            adapter.check(
                destination_root=destination,
                job_id="stage6-hardware",
                marker_uuid=marker,
                mode="full",
            )
    finally:
        shutil.rmtree(source, ignore_errors=True)
        shutil.rmtree(destination, ignore_errors=True)
