"""Validated source-root-relative exclude patterns shared by all adapters."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePath, PureWindowsPath


def validate_exclude_pattern(value: str) -> str:
    path = PureWindowsPath(value)
    if (
        not value
        or path.is_absolute()
        or path.drive
        or value.startswith(("\\", "/"))
        or ".." in path.parts
        or not path.parts
    ):
        raise ValueError("excludes must be non-empty relative Windows patterns")
    if value.startswith("!") or any(character in value for character in "[]"):
        raise ValueError("excludes cannot contain negation or character classes")
    if any("**" in part and part != "**" for part in path.parts):
        raise ValueError("double wildcard must be a complete path component")
    return value


def exclude_matches(pattern: str, relative_path: str) -> bool:
    """Return whether a file/directory relative to source root is excluded."""
    pattern_parts = tuple(part.casefold() for part in PureWindowsPath(pattern).parts)
    path_parts = tuple(part.casefold() for part in PureWindowsPath(relative_path).parts)
    return _match(pattern_parts, path_parts, 0, 0)


def restic_exclude_pattern(pattern: str, source_root: str | PurePath) -> str:
    """Translate a BBS pattern to the absolute Windows source path restic matches."""
    source = PureWindowsPath(source_root)
    if not source.is_absolute() or not source.drive:
        raise ValueError("restic source root must be an absolute Windows path")
    return str(source.joinpath(PureWindowsPath(pattern))).replace("\\", "/")


def _match(
    pattern: tuple[str, ...], path: tuple[str, ...], pattern_index: int, path_index: int
) -> bool:
    if pattern_index == len(pattern):
        return True
    component = pattern[pattern_index]
    if component == "**":
        return _match(pattern, path, pattern_index + 1, path_index) or (
            path_index < len(path) and _match(pattern, path, pattern_index, path_index + 1)
        )
    if path_index == len(path) or not fnmatchcase(path[path_index], component):
        return False
    return _match(pattern, path, pattern_index + 1, path_index + 1)
