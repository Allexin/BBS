"""Executor command-line surface; operation bodies arrive in later stages."""

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup-executor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "prune", "restore-test", "repair-mirror", "recover"):
        child = subparsers.add_parser(command)
        child.add_argument("--run-id", required=True)
        child.add_argument("--job", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--run-id", required=True)
    check.add_argument("--job", required=True)
    check.add_argument("--mode", choices=("metadata", "sample", "full"), required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--run-id", required=True)
    restore.add_argument("--job", required=True)
    restore.add_argument("--request-file", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--job", required=True)
    subparsers.add_parser("validate-smart-config")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
