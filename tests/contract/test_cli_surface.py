import argparse

import pytest

from backup_system.ctl.cli import build_parser as ctl_parser
from backup_system.executor.cli import build_parser as executor_parser
from backup_system.manager.cli import build_parser as manager_parser


@pytest.mark.parametrize(
    ("parser", "arguments"),
    [
        (manager_parser(), ["--config", "C:\\config\\manager.yaml", "--validate-only"]),
        (executor_parser(), ["validate", "--job", "data"]),
        (
            executor_parser(),
            ["run", "--run-id", "00000000-0000-4000-8000-000000000000", "--job", "data"],
        ),
        (ctl_parser(), ["config", "validate"]),
        (ctl_parser(), ["queue", "remove", "00000000-0000-4000-8000-000000000000"]),
    ],
)
def test_documented_cli_shapes_parse(parser: argparse.ArgumentParser, arguments: list[str]) -> None:
    parser.parse_args(arguments)
