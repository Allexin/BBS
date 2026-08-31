from pathlib import Path
from types import SimpleNamespace

import pytest

from backup_system.executor.cancellation import CancellationToken
from backup_system.executor.restic_process import (
    ResticProcess,
    ResticProcessError,
    _classify_fault,
    _fault_diagnostic,
)


def test_only_supported_major_minor_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backup_system.executor.restic_process.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="restic 0.19.1 compiled"),
    )
    assert ResticProcess(Path("restic.exe"), CancellationToken()).verify_version() == (0, 19, 1)


def test_unconfirmed_version_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backup_system.executor.restic_process.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="restic 0.20.0 compiled"),
    )
    with pytest.raises(ResticProcessError, match="unsupported"):
        ResticProcess(Path("restic.exe"), CancellationToken()).verify_version()


def test_structured_archival_error_is_source_failure() -> None:
    assert (
        _classify_fault("stderr", "ignored", {"message_type": "error", "during": "archival"})
        == "source_read_error"
    )


def test_private_fault_diagnostic_preserves_structured_restic_reason() -> None:
    diagnostic = _fault_diagnostic(
        "ignored",
        ({"message_type": "error", "during": "archival", "error": "access denied"},),
    )
    assert '"error":"access denied"' in diagnostic


def test_pinned_repository_diagnostics_are_narrow() -> None:
    repository = r"R:\repository"
    line = rf"Save returned error, retrying after 1s: write {repository}\data: I/O error"
    assert _classify_fault("stderr", line, None, repository) == "repository_io_error"
    assert _classify_fault("stderr", line, None, r"X:\other") is None
    assert (
        _classify_fault("stderr", "write: There is not enough space on the disk.", None)
        == "repository_out_of_space"
    )
