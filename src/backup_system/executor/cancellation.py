"""Executor cooperative cancellation token driven only by its inherited stdin pipe."""

from __future__ import annotations

from threading import Event, Lock, Thread
from typing import BinaryIO

MAX_CANCEL_FRAME_BYTES = 64


class CancellationRequested(RuntimeError):
    pass


class CancellationProtocolError(RuntimeError):
    pass


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self) -> None:
        self._event.set()

    def raise_if_requested(self) -> None:
        if self.requested:
            raise CancellationRequested("executor cancellation was requested")


class StdinCancellationMonitor:
    def __init__(self, stream: BinaryIO, token: CancellationToken) -> None:
        self._stream = stream
        self._token = token
        self._error: CancellationProtocolError | None = None
        self._error_lock = Lock()
        self._thread: Thread | None = None

    @property
    def protocol_error(self) -> CancellationProtocolError | None:
        with self._error_lock:
            return self._error

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("cancellation monitor was already started")
        self._thread = Thread(target=self._run, name="executor-cancel", daemon=True)
        self._thread.start()

    def wait(self, timeout_seconds: float) -> bool:
        thread = self._thread
        if thread is None:
            raise RuntimeError("cancellation monitor was not started")
        thread.join(timeout=timeout_seconds)
        return not thread.is_alive()

    def _run(self) -> None:
        frame = self._stream.readline(MAX_CANCEL_FRAME_BYTES + 1)
        if frame == b"":
            return
        if len(frame) > MAX_CANCEL_FRAME_BYTES or not frame.endswith(b"\n"):
            self._set_error("cancellation frame exceeds limit or is incomplete")
            return
        if frame not in {b"cancel\n", b"cancel\r\n"}:
            self._set_error("unknown cancellation frame")
            return
        self._token.request()

    def _set_error(self, message: str) -> None:
        with self._error_lock:
            self._error = CancellationProtocolError(message)
