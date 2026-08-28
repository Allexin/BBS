"""Pure mirror scanning, planning and capacity preflight."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Protocol


class MirrorPlanError(RuntimeError):
    pass


class MirrorOutOfSpaceError(MirrorPlanError):
    pass


class PathKeyProvider(Protocol):
    def key(self, relative_path: str) -> str: ...


class PortableWindowsPathKeys:
    """Windows-like fallback used by pure planning tests."""

    def key(self, relative_path: str) -> str:
        path = PureWindowsPath(relative_path)
        if path.is_absolute() or path.drive or ".." in path.parts or not path.parts:
            raise MirrorPlanError(f"unsafe relative path: {relative_path!r}")
        return "\\".join(path.parts).casefold()


@dataclass(frozen=True, slots=True)
class ScannedFile:
    path_key: str
    relative_path: str
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ScanResult:
    files: dict[str, ScannedFile]
    directories: tuple[str, ...]
    total_bytes: int
    skipped_reparse_points: int


class PlanAction(StrEnum):
    DELETE = "delete"
    SHRINK = "shrink"
    REPLACE = "replace"
    GROW = "grow"


@dataclass(frozen=True, slots=True)
class PlannedFile:
    action: PlanAction
    path_key: str
    relative_path: str
    source_size_bytes: int | None
    destination_size_bytes: int | None
    source_mtime_ns: int | None


@dataclass(frozen=True, slots=True)
class MirrorPlan:
    files: tuple[PlannedFile, ...]
    destination_directories: tuple[str, ...]
    current_mirror_size: int
    planned_mirror_size: int
    largest_copy_size: int

    @property
    def required_peak_mirror_size(self) -> int:
        return max(self.current_mirror_size, self.planned_mirror_size) + self.largest_copy_size


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    required_peak_mirror_size: int
    available_to_mirror_bytes: int
    projected_remaining_free_bytes: int
    positive_growth_bytes: int
    growth_warning: bool


def scan_tree(
    root: Path,
    *,
    excludes: tuple[str, ...] = (),
    path_keys: PathKeyProvider | None = None,
    reserved_root: str | None = None,
) -> ScanResult:
    keys = path_keys or PortableWindowsPathKeys()
    if not root.is_dir():
        raise MirrorPlanError(f"scan root is unavailable: {root}")
    excluded = tuple(_parts(value) for value in excludes)
    files: dict[str, ScannedFile] = {}
    directories: list[str] = []
    total = 0
    skipped = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise MirrorPlanError(f"cannot scan directory: {directory}") from error
        for entry in entries:
            relative = str(PureWindowsPath(Path(entry.path).relative_to(root)))
            parts = _parts(relative)
            if (
                reserved_root is not None
                and len(parts) == 1
                and parts[0] == reserved_root.casefold()
            ) or any(parts[: len(prefix)] == prefix for prefix in excluded):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise MirrorPlanError(f"cannot stat object: {relative}") from error
            attributes = getattr(info, "st_file_attributes", 0)
            if stat.S_ISLNK(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                skipped += 1
                continue
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                stack.append(Path(entry.path))
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            path_key = keys.key(relative)
            if path_key in files:
                raise MirrorPlanError(f"Windows path collision: {relative!r}")
            item = ScannedFile(path_key, relative, info.st_size, info.st_mtime_ns)
            files[path_key] = item
            total += info.st_size
    return ScanResult(files, tuple(directories), total, skipped)


def build_plan(
    source: ScanResult,
    destination: ScanResult,
    *,
    unchanged_path_keys: frozenset[str] = frozenset(),
) -> MirrorPlan:
    actions: list[PlannedFile] = []
    for key, destination_only in destination.files.items():
        if key not in source.files:
            actions.append(
                PlannedFile(
                    PlanAction.DELETE,
                    key,
                    destination_only.relative_path,
                    None,
                    destination_only.size_bytes,
                    None,
                )
            )
    for key, item in source.files.items():
        destination_file = destination.files.get(key)
        if destination_file is not None and key in unchanged_path_keys:
            continue
        old_size = None if destination_file is None else destination_file.size_bytes
        if old_size is None or item.size_bytes > old_size:
            action = PlanAction.GROW
        elif item.size_bytes < old_size:
            action = PlanAction.SHRINK
        else:
            action = PlanAction.REPLACE
        actions.append(
            PlannedFile(action, key, item.relative_path, item.size_bytes, old_size, item.mtime_ns)
        )
    order = {PlanAction.DELETE: 0, PlanAction.SHRINK: 1, PlanAction.REPLACE: 2, PlanAction.GROW: 3}
    actions.sort(key=lambda item: (order[item.action], item.path_key))
    copied_sizes = [
        item.source_size_bytes or 0 for item in actions if item.action != PlanAction.DELETE
    ]
    source_directory_keys = {_parts(value) for value in source.directories}
    removable_directories = [
        value for value in destination.directories if _parts(value) not in source_directory_keys
    ]
    return MirrorPlan(
        files=tuple(actions),
        destination_directories=tuple(
            sorted(removable_directories, key=lambda value: (-len(_parts(value)), value))
        ),
        current_mirror_size=destination.total_bytes,
        planned_mirror_size=source.total_bytes,
        largest_copy_size=max(copied_sizes, default=0),
    )


def assess_capacity(plan: MirrorPlan, *, volume_free_bytes: int) -> CapacityAssessment:
    available = volume_free_bytes + plan.current_mirror_size
    required = plan.required_peak_mirror_size
    if required > available:
        raise MirrorOutOfSpaceError(
            f"mirror requires {required} bytes but only {available} are available"
        )
    growth = max(0, plan.planned_mirror_size - plan.current_mirror_size)
    remaining = volume_free_bytes - growth
    return CapacityAssessment(required, available, remaining, growth, growth > remaining * 0.10)


def _parts(value: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PureWindowsPath(value).parts)
