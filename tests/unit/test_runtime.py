from pathlib import Path

import pytest

from backup_system.common.runtime import RuntimeRootError, discover_runtime_root


def test_runtime_root_requires_marker_next_to_venv(tmp_path: Path) -> None:
    executable = tmp_path / ".venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    with pytest.raises(RuntimeRootError):
        discover_runtime_root(executable)
    (tmp_path / "backup-system.root").touch()
    assert discover_runtime_root(executable) == tmp_path
