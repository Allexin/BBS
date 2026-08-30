from pathlib import Path

from backup_system.deployment.update import replace_tree_contents


def test_replace_tree_contents_preserves_target_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "new.txt").write_text("new", encoding="ascii")
    (source / "nested").mkdir()
    (source / "nested" / "file.txt").write_text("nested", encoding="ascii")
    (target / "old.txt").write_text("old", encoding="ascii")
    target_identity = target.stat().st_ino

    replace_tree_contents(source, target)

    assert target.stat().st_ino == target_identity
    assert not (target / "old.txt").exists()
    assert (target / "new.txt").read_text(encoding="ascii") == "new"
    assert (target / "nested" / "file.txt").read_text(encoding="ascii") == "nested"
