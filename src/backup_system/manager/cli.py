"""Manager service command-line surface."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from backup_system.common.config_io import ConfigLoadError, validate_config_tree
from backup_system.common.exit_codes import ManagerExitCode
from backup_system.manager.bootstrap import write_bootstrap_failure
from backup_system.manager.service import run_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup-manager")
    parser.add_argument("--config", required=True)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    config_path = Path(arguments.config)
    try:
        manager_config, smart_config = validate_config_tree(config_path)
    except ConfigLoadError as error:
        diagnostic = str(error)
        print(diagnostic, file=sys.stderr)
        try:
            write_bootstrap_failure(
                config_path,
                exit_code=int(ManagerExitCode.CONFIG_INVALID),
                diagnostic=diagnostic,
            )
        except (OSError, ValueError) as log_error:
            print(f"cannot persist bootstrap diagnostic: {log_error}", file=sys.stderr)
            return ManagerExitCode.BOOTSTRAP_ERROR
        return ManagerExitCode.CONFIG_INVALID
    if arguments.validate_only:
        return ManagerExitCode.SUCCESS
    try:
        asyncio.run(run_service(config_path, manager_config, smart_config))
    except (OSError, RuntimeError, ValueError) as error:
        diagnostic = f"manager bootstrap failed: {error}"
        print(diagnostic, file=sys.stderr)
        try:
            write_bootstrap_failure(
                config_path,
                exit_code=int(ManagerExitCode.BOOTSTRAP_ERROR),
                diagnostic=diagnostic,
            )
        except (OSError, ValueError) as log_error:
            print(f"cannot persist bootstrap diagnostic: {log_error}", file=sys.stderr)
        return ManagerExitCode.BOOTSTRAP_ERROR
    return ManagerExitCode.SUCCESS
