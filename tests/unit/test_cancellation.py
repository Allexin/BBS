import io

import pytest

from backup_system.executor.cancellation import (
    CancellationRequested,
    CancellationToken,
    StdinCancellationMonitor,
)


def _monitor(payload: bytes) -> tuple[CancellationToken, StdinCancellationMonitor]:
    token = CancellationToken()
    monitor = StdinCancellationMonitor(io.BytesIO(payload), token)
    monitor.start()
    assert monitor.wait(1)
    return token, monitor


def test_exact_cancel_frame_sets_token() -> None:
    token, monitor = _monitor(b"cancel\n")
    assert token.requested
    assert monitor.protocol_error is None
    with pytest.raises(CancellationRequested):
        token.raise_if_requested()


def test_eof_is_not_treated_as_successful_cancel() -> None:
    token, monitor = _monitor(b"")
    assert not token.requested
    assert monitor.protocol_error is None


@pytest.mark.parametrize("payload", [b"stop\n", b"cancel", b"x" * 65 + b"\n"])
def test_unknown_incomplete_or_oversized_frame_is_rejected(payload: bytes) -> None:
    token, monitor = _monitor(payload)
    assert not token.requested
    assert monitor.protocol_error is not None
