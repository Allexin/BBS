"""Disposable process used only by the Stage 9 NSSM acceptance."""

import argparse
import os
import signal
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("cleanup", "config-invalid"))
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=12.0)
    args = parser.parse_args()
    if args.mode == "config-invalid":
        with args.marker.open("a", encoding="ascii") as stream:
            stream.write("start\n")
            stream.flush()
            os.fsync(stream.fileno())
        return 40

    stopping = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopping
        del signum, frame
        stopping = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)
    args.marker.with_suffix(".ready").write_text("ready\n", encoding="ascii")
    while not stopping:
        time.sleep(0.05)
    time.sleep(args.delay)
    args.marker.write_text("cleanup-complete\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
