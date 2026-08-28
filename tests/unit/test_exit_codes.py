from backup_system.common.exit_codes import ExecutorExitCode


def test_executor_exit_codes_are_stable() -> None:
    assert ExecutorExitCode.SUCCESS == 0
    assert ExecutorExitCode.CONFIG_INVALID == 20
    assert ExecutorExitCode.INTERNAL_ERROR == 30
