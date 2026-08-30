import json
from pathlib import Path

import pytest

from backup_system.deployment.manifest import (
    DeploymentManifestError,
    load_deployment_manifest,
    stage_release,
)


def _manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "application_files": ["pyproject.toml"],
                "application_trees": ["src"],
                "web_trees": ["web"],
                "documentation_trees": ["docs"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_stage_copies_only_manifest_content_and_never_data(tmp_path: Path) -> None:
    source = tmp_path / "dev"
    for directory in ("src", "web", "docs", ".git", ".venv", "data"):
        (source / directory).mkdir(parents=True)
        (source / directory / "item.txt").write_text(directory, encoding="utf-8")
    (source / "pyproject.toml").write_text("project", encoding="utf-8")
    manifest = load_deployment_manifest(_manifest(source / "manifest.json"))

    staging = tmp_path / "staging"
    stage_release(source, staging, manifest)

    assert (staging / "app/src/item.txt").read_text(encoding="utf-8") == "src"
    assert (staging / "web/item.txt").is_file()
    assert (staging / "app/docs/item.txt").is_file()
    assert not (staging / "data").exists()
    assert not (staging / "app/.git").exists()
    assert not (staging / "app/.venv").exists()


def test_manifest_rejects_parent_escape(tmp_path: Path) -> None:
    path = _manifest(tmp_path / "manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["application_files"] = ["../secret"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DeploymentManifestError):
        load_deployment_manifest(path)
