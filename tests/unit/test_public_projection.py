import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from backup_system.manager.public_projection import (
    HealthProjection,
    ProjectionPublisher,
    StatusProjection,
)


def _projections(generation_id: UUID) -> tuple[StatusProjection, HealthProjection]:
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    status = StatusProjection(
        generation_id=generation_id,
        generated_at=now,
        overall_health="healthy",
        backup_disk_state="offline",
        operations=(),
        jobs=(),
        disks=(),
        volumes=(),
        health_issues=(),
    )
    health = HealthProjection(
        generation_id=generation_id,
        generated_at=now,
        manager_state="idle",
        manager_started_at=now,
        version="0.1.0",
        status_stale_after_seconds=60,
    )
    return status, health


def test_projection_rejects_unknown_fields_and_mismatched_generation(tmp_path: Path) -> None:
    status, health = _projections(uuid4())
    with pytest.raises(ValidationError, match="extra_forbidden"):
        StatusProjection.model_validate({**status.model_dump(), "repository_path": "C:\\secret"})
    with pytest.raises(ValueError, match="generation IDs"):
        ProjectionPublisher(tmp_path).publish(
            status, health.model_copy(update={"generation_id": uuid4()})
        )


def test_publisher_writes_utf8_json_and_health_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, health = _projections(uuid4())
    replacements: list[str] = []
    original_replace = os.replace

    def observe(source: Path, destination: Path) -> None:
        replacements.append(Path(destination).name)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", observe)
    ProjectionPublisher(tmp_path).publish(status, health)
    assert replacements == ["status.json", "health.json"]
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))[
        "generation_id"
    ] == str(status.generation_id)


def test_failed_replace_preserves_previous_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = ProjectionPublisher(tmp_path)
    old_status, old_health = _projections(uuid4())
    publisher.publish(old_status, old_health)
    original = (tmp_path / "status.json").read_bytes()

    def fail(source: Path, destination: Path) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr(os, "replace", fail)
    new_status, new_health = _projections(uuid4())
    with pytest.raises(OSError, match="simulated"):
        publisher.publish(new_status, new_health)
    assert (tmp_path / "status.json").read_bytes() == original
    assert json.loads(original)["generation_id"] == str(old_status.generation_id)
    assert not list(tmp_path.glob("*.tmp"))


def test_publisher_retries_transient_windows_file_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, health = _projections(uuid4())
    original_replace = os.replace
    attempts = 0

    def transient_lock(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            error = PermissionError("temporarily locked")
            error.winerror = 5
            raise error
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", transient_lock)
    ProjectionPublisher(tmp_path, retry_delay_seconds=0).publish(status, health)
    assert attempts == 4
    assert (tmp_path / "health.json").is_file()
