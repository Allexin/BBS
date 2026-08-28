from pathlib import Path

import pytest

from backup_system.executor.machine_lock import (
    ExecutorAlreadyRunningError,
    MachineExecutorLock,
)


def test_second_executor_cannot_acquire_machine_lock(tmp_path: Path) -> None:
    path = tmp_path / "executor.lock"
    with (
        MachineExecutorLock(path),
        pytest.raises(ExecutorAlreadyRunningError),
        MachineExecutorLock(path),
    ):
        pytest.fail("contended lock was acquired")


def test_lock_can_be_reacquired_after_release(tmp_path: Path) -> None:
    path = tmp_path / "executor.lock"
    with MachineExecutorLock(path):
        pass
    with MachineExecutorLock(path):
        pass
