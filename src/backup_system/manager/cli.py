"""Manager service command-line surface."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from backup_system.common.config_io import ConfigLoadError, validate_config_tree
from backup_system.common.exit_codes import ExecutorExitCode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup-manager")
    parser.add_argument("--config", required=True)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.validate_only:
        try:
            validate_config_tree(Path(arguments.config))
        except ConfigLoadError as error:
            print(str(error), file=sys.stderr)
            return ExecutorExitCode.CONFIG_INVALID
    return 0
