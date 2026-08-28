from pathlib import Path

import pytest

from backup_system.manager.layout import RuntimeLayout, initialize_data_layout


def test_layout_initialization_is_fixed_and_idempotent(tmp_path: Path) -> None:
    (tmp_path / "backup-system.root").touch()
    layout = RuntimeLayout(tmp_path)

    initialize_data_layout(layout)
    sentinel = layout.commands_accepted / "existing.json"
    sentinel.write_text("keep", encoding="utf-8")
    initialize_data_layout(layout)

    assert all(path.is_dir() for path in layout.mutable_directories())
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert layout.database == tmp_path / "data" / "state" / "manager.sqlite3"


def test_layout_requires_runtime_marker(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="marker is missing"):
        initialize_data_layout(RuntimeLayout(tmp_path))
