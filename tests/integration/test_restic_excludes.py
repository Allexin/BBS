import json
import subprocess
from pathlib import Path

import pytest

from backup_system.common.excludes import restic_exclude_pattern

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESTIC = (
    PROJECT_ROOT
    / ".tools"
    / "restic-0.19.1"
    / "restic_0.19.1_windows_amd64.exe"
)


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(RESTIC), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result


@pytest.mark.integration
def test_recursive_exclude_is_anchored_to_windows_source_root(tmp_path: Path) -> None:
    if not RESTIC.is_file():
        pytest.skip("pinned restic executable is unavailable")
    source = tmp_path / "source"
    repository = tmp_path / "repository"
    excluded = (
        source / "Audiolibraries" / "Rutracker" / "audio" / "book" / "cache.OGG"
    )
    included = source / "Audiolibraries" / "Rutracker" / "audio" / "book" / "keep.mp3"
    same_name_elsewhere = source / "other" / "audio" / "book" / "keep.ogg"
    for path in (excluded, included, same_name_elsewhere):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    exclude_file = tmp_path / "excludes.txt"
    exclude_file.write_text(
        restic_exclude_pattern(
            r"Audiolibraries\Rutracker\audio\**\*.ogg", source
        )
        + "\n",
        encoding="utf-8",
    )

    base = ("--repo", str(repository), "--insecure-no-password")
    _run(*base, "init", "--json")
    _run(*base, "backup", "--json", "--iexclude-file", str(exclude_file), str(source))
    listing = _run(*base, "ls", "latest", "--json")
    paths = {
        str(record["path"])
        for line in listing.stdout.splitlines()
        if (record := json.loads(line)).get("struct_type") == "node"
        and record.get("type") == "file"
    }

    assert not any(path.casefold().endswith("cache.ogg") for path in paths)
    assert sum(path.casefold().endswith("keep.mp3") for path in paths) == 1
    assert sum(path.casefold().endswith("keep.ogg") for path in paths) == 1
