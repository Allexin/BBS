"""Machine-wide single-executor lock released automatically by Windows on exit."""

from __future__ import annotations

import msvcrt
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class ExecutorAlreadyRunningError(RuntimeError):
    pass


class MachineExecutorLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: BinaryIO | None = None

    def __enter__(self) -> MachineExecutorLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._path.open("a+b", buffering=0)
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b"\0")
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            stream.close()
            raise ExecutorAlreadyRunningError("another executor process holds the lock") from error
        self._stream = stream
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            stream.close()
