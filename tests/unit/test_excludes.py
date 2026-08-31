import pytest

from backup_system.common.excludes import (
    exclude_matches,
    restic_exclude_pattern,
    validate_exclude_pattern,
)


@pytest.mark.parametrize(
    "pattern",
    [
        "Cache",
        r"Audiolibraries\Rutracker\audio\*\*.ogg",
        r"Audiolibraries\Rutracker\audio\**\*.ogg",
        r"data\file-?.tmp",
    ],
)
def test_valid_relative_patterns(pattern: str) -> None:
    assert validate_exclude_pattern(pattern) == pattern


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        r"M:\cache",
        r"\cache",
        r"data\..\cache",
        r"data\foo**\bar",
        r"data\**.ogg",
        r"data\[ab].tmp",
        "!cache",
    ],
)
def test_unsafe_or_ambiguous_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(ValueError):
        validate_exclude_pattern(pattern)


def test_patterns_are_root_relative_case_insensitive_and_component_aware() -> None:
    assert exclude_matches("Cache", r"CACHE\nested\file.bin")
    assert not exclude_matches("Cache", r"other\Cache\file.bin")
    pattern = r"Audiolibraries\Rutracker\audio\*\*.ogg"
    assert exclude_matches(pattern, r"Audiolibraries\Rutracker\audio\book\one.OGG")
    assert not exclude_matches(pattern, r"Audiolibraries\Rutracker\audio\a\b\one.ogg")
    recursive = r"Audiolibraries\Rutracker\audio\**\*.ogg"
    assert exclude_matches(recursive, r"Audiolibraries\Rutracker\audio\one.ogg")
    assert exclude_matches(recursive, r"Audiolibraries\Rutracker\audio\a\b\one.ogg")
    assert not exclude_matches(recursive, r"other\audio\a\one.ogg")
    assert not exclude_matches(recursive, r"Audiolibraries\Rutracker\audio\a\one.mp3")


def test_restic_pattern_is_anchored_and_uses_portable_separators() -> None:
    assert (
        restic_exclude_pattern(r"Cache\**\*.tmp", r"S:\Data\source")
        == "S:/Data/source/Cache/**/*.tmp"
    )
    assert (
        restic_exclude_pattern(
            r"Cache\*.tmp",
            r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy7\Data",
        )
        == "//?/GLOBALROOT/Device/HarddiskVolumeShadowCopy7/Data/Cache/*.tmp"
    )
