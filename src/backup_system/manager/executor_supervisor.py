"""Job-contained relay that starts executor only after manager authorization."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Sequence


def _relay_cancel(process: subprocess.Popen[bytes]) -> None:
    frame = sys.stdin.buffer.readline(64)
    child_stdin = process.stdin
    if child_stdin is None:
        return
    try:
        if frame != b"cancel\n":
            return
        try:
            child_stdin.write(frame)
            child_stdin.flush()
        except BrokenPipeError:
            pass
    finally:
        child_stdin.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "--" or len(arguments) == 1:
        print("executor supervisor requires an argv after --", file=sys.stderr)
        return 2
    if sys.stdin.buffer.readline(64) != b"start\n":
        print("executor supervisor start protocol failed", file=sys.stderr)
        return 2
    process = subprocess.Popen(
        arguments[1:],
        stdin=subprocess.PIPE,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
        shell=False,
    )
    relay = threading.Thread(target=_relay_cancel, args=(process,))
    relay.start()
    exit_code = process.wait()
    relay.join()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
