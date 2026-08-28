"""Discovery of the fixed Stable runtime root."""

from pathlib import Path


class RuntimeRootError(RuntimeError):
    """The process is not running from an unambiguous Stable layout."""


def discover_runtime_root(executable: Path) -> Path:
    executable = executable.resolve(strict=False)
    candidates = [parent for parent in executable.parents if parent.name.casefold() == ".venv"]
    marked = [
        candidate.parent
        for candidate in candidates
        if (candidate.parent / "backup-system.root").is_file()
    ]
    if len(marked) != 1:
        raise RuntimeRootError("expected exactly one marked runtime root containing .venv")
    return marked[0]
