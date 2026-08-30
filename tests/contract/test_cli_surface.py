import argparse

import pytest

from backup_system.common.commands import RunCommand
from backup_system.ctl.cli import build_parser as ctl_parser
from backup_system.ctl.cli import command_from_arguments
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
        (
            ctl_parser(),
            [
                "restore",
                "data",
                "--version",
                "latest",
                "--path",
                ".",
                "--target",
                "C:\\Restore",
            ],
        ),
    ],
)
def test_documented_cli_shapes_parse(parser: argparse.ArgumentParser, arguments: list[str]) -> None:
    parser.parse_args(arguments)


def test_invalid_job_id_is_rejected_at_cli_boundary() -> None:
    with pytest.raises(SystemExit):
        ctl_parser().parse_args(["run", "../data"])


def test_non_uuid4_operation_id_is_rejected_at_cli_boundary() -> None:
    with pytest.raises(SystemExit):
        ctl_parser().parse_args(["queue", "remove", "00000000-0000-1000-8000-000000000000"])


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "data"],
        ["check", "data", "--mode", "full"],
        ["restore-test", "data"],
        ["repair-mirror", "data"],
        ["recover", "data"],
        ["cancel-current"],
        ["queue", "remove", "00000000-0000-4000-8000-000000000000"],
    ],
)
def test_mutating_cli_builds_a_typed_spool_command(arguments: list[str]) -> None:
    parsed = ctl_parser().parse_args(arguments)
    assert command_from_arguments(parsed) is not None


def test_read_only_cli_does_not_build_a_spool_command() -> None:
    assert command_from_arguments(ctl_parser().parse_args(["status"])) is None


def test_generic_run_leaves_operation_resolution_to_manager() -> None:
    command = command_from_arguments(ctl_parser().parse_args(["run", "disk-health"]))
    assert isinstance(command, RunCommand)
    assert command.operation is None
