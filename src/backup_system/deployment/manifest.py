"""Strict release manifest and guarded copy into an isolated staging tree."""

from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

_IGNORED_RELEASE_DIRECTORIES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
_IGNORED_RELEASE_SUFFIXES = frozenset({".pyc", ".pyo"})


class DeploymentManifestError(ValueError):
    pass


class DeploymentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1, le=1)
    application_files: tuple[str, ...]
    application_trees: tuple[str, ...]
    web_trees: tuple[str, ...]
    documentation_trees: tuple[str, ...]

    @field_validator(
        "application_files", "application_trees", "web_trees", "documentation_trees"
    )
    @classmethod
    def safe_relative_entries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("manifest entries must be unique")
        for value in values:
            path = PurePosixPath(value)
            if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("manifest entries must be safe relative POSIX paths")
            if any(character in value for character in "*?"):
                raise ValueError("manifest entries cannot contain wildcards")
        return values


def load_deployment_manifest(path: Path) -> DeploymentManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return DeploymentManifest.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise DeploymentManifestError(f"invalid deployment manifest {path}: {error}") from error


def stage_release(source: Path, staging: Path, manifest: DeploymentManifest) -> None:
    source = source.resolve(strict=True)
    staging.mkdir(parents=True, exist_ok=False)
    application = staging / "app"
    application.mkdir()
    for relative in manifest.application_files:
        _copy_file(source, relative, application / relative)
    for relative in manifest.application_trees:
        _copy_tree(source, relative, application / relative)
    for relative in manifest.web_trees:
        _copy_tree(source, relative, staging / relative)
    for relative in manifest.documentation_trees:
        _copy_tree(source, relative, application / relative)


def _copy_file(source: Path, relative: str, target: Path) -> None:
    path = _guard_source(source, relative)
    if not path.is_file():
        raise DeploymentManifestError(f"manifest file is missing: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target, follow_symlinks=False)


def _copy_tree(source: Path, relative: str, target: Path) -> None:
    path = _guard_source(source, relative)
    if not path.is_dir():
        raise DeploymentManifestError(f"manifest tree is missing: {relative}")
    for child in path.rglob("*"):
        if _is_release_artifact(child, root=path):
            continue
        if child.is_symlink():
            raise DeploymentManifestError(f"release tree contains a symlink: {child}")
    shutil.copytree(
        path,
        target,
        copy_function=shutil.copy2,
        ignore=_ignore_release_artifacts,
    )


def _ignore_release_artifacts(directory: str, names: list[str]) -> set[str]:
    del directory
    return {
        name
        for name in names
        if name in _IGNORED_RELEASE_DIRECTORIES
        or Path(name).suffix.casefold() in _IGNORED_RELEASE_SUFFIXES
    }


def _is_release_artifact(path: Path, *, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(part in _IGNORED_RELEASE_DIRECTORIES for part in relative.parts) or (
        path.suffix.casefold() in _IGNORED_RELEASE_SUFFIXES
    )


def _guard_source(source: Path, relative: str) -> Path:
    path = source.joinpath(*PurePosixPath(relative).parts)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(source):
        raise DeploymentManifestError("manifest entry escapes source root")
    if path.is_symlink():
        raise DeploymentManifestError(f"release entry is a symlink: {relative}")
    return path
