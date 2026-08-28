import os
from pathlib import Path

import pytest

from backup_system.executor.mirror_plan import (
    MirrorOutOfSpaceError,
    PlanAction,
    ScannedFile,
    ScanResult,
    assess_capacity,
    build_plan,
    scan_tree,
)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _scan_from_sizes(sizes: dict[str, int]) -> ScanResult:
    files = {name: ScannedFile(name, name, size, 1) for name, size in sizes.items()}
    return ScanResult(files, (), sum(sizes.values()), 0)


def test_scan_plan_orders_deletes_and_replacements_and_respects_excludes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _write(source / "same.bin", b"same")
    _write(source / "smaller.bin", b"x")
    _write(source / "larger.bin", b"larger")
    _write(source / "new.bin", b"new")
    _write(source / "excluded" / "secret.bin", b"secret")
    _write(destination / "same.bin", b"same")
    _write(destination / "smaller.bin", b"long")
    _write(destination / "larger.bin", b"x")
    _write(destination / "old.bin", b"old")
    (destination / "obsolete-empty").mkdir()
    (destination / "kept-empty").mkdir()
    (source / "kept-empty").mkdir()
    _write(destination / ".backup-system" / "catalog.sqlite3", b"catalog")

    source_scan = scan_tree(source, excludes=("excluded",))
    destination_scan = scan_tree(destination, reserved_root=".backup-system")
    plan = build_plan(source_scan, destination_scan, unchanged_path_keys=frozenset({"same.bin"}))

    assert [(item.action, item.relative_path) for item in plan.files] == [
        (PlanAction.DELETE, "old.bin"),
        (PlanAction.SHRINK, "smaller.bin"),
        (PlanAction.GROW, "larger.bin"),
        (PlanAction.GROW, "new.bin"),
    ]
    assert plan.current_mirror_size == 12
    assert plan.planned_mirror_size == 14
    assert plan.largest_copy_size == 6
    assert plan.required_peak_mirror_size == 20
    assert plan.destination_directories == ("obsolete-empty",)


def test_scan_does_not_follow_reparse_or_symlink(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = tmp_path / "outside"
    _write(target / "data.bin", b"outside")
    try:
        os.symlink(target, root / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = scan_tree(root)

    assert result.files == {}
    assert result.skipped_reparse_points == 1


def test_capacity_rejects_before_mutation_and_reports_growth_warning() -> None:
    plan = build_plan(_scan_from_sizes({"new.bin": 120}), _scan_from_sizes({"old.bin": 100}))

    with pytest.raises(MirrorOutOfSpaceError):
        assess_capacity(plan, volume_free_bytes=119)

    assessment = assess_capacity(plan, volume_free_bytes=200)
    assert assessment.projected_remaining_free_bytes == 180
    assert assessment.positive_growth_bytes == 20
    assert assessment.growth_warning is True
