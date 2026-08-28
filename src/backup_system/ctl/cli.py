"""Local control command surface; spool writes arrive in stage 2."""

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backupctl")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("cancel-current")
    config = subparsers.add_parser("config")
    config.add_subparsers(dest="config_command", required=True).add_parser("validate")
    jobs = subparsers.add_parser("jobs")
    jobs.add_subparsers(dest="jobs_command", required=True).add_parser("list")
    queue = subparsers.add_parser("queue")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    queue_commands.add_parser("list")
    remove = queue_commands.add_parser("remove")
    remove.add_argument("operation_id")
    for command in ("run", "restore-test", "repair-mirror", "recover"):
        child = subparsers.add_parser(command)
        child.add_argument("job_id")
    check = subparsers.add_parser("check")
    check.add_argument("job_id")
    check.add_argument("--mode", choices=("subset", "full"), required=True)
    disk = subparsers.add_parser("disk")
    disk_status = disk.add_subparsers(dest="disk_command", required=True).add_parser("status")
    disk_status.add_argument("job_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
