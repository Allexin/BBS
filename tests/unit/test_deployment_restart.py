from datetime import UTC, datetime, timedelta
from pathlib import Path

from backup_system.deployment.restart import _is_fresh_response


def test_stable_restart_launcher_uses_only_stable_runtime() -> None:
    launcher = (Path(__file__).parents[2] / "restart-bbs.bat").read_text(encoding="utf-8")
    assert "%~dp0.venv\\Scripts\\python.exe" in launcher
    assert "backup_system.deployment.restart" in launcher
    assert "-Verb RunAs" in launcher
    assert "WindowsBuiltInRole]::Administrator" in launcher
    assert "poc" not in launcher.casefold()


def test_fresh_response_requires_new_process_and_matching_projection() -> None:
    requested = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    health = {
        "generation_id": "generation-2",
        "manager_state": "idle",
        "manager_started_at": requested.isoformat(),
        "generated_at": (requested + timedelta(seconds=1)).isoformat(),
    }
    status = {"generation_id": "generation-2"}
    assert _is_fresh_response(
        health,
        status,
        previous_started=(requested - timedelta(hours=1)).isoformat(),
        requested_at=requested,
    )
    assert not _is_fresh_response(
        health,
        {"generation_id": "changing"},
        previous_started=None,
        requested_at=requested,
    )
    assert not _is_fresh_response(
        health,
        status,
        previous_started=requested.isoformat(),
        requested_at=requested,
    )


def test_stale_or_stopping_health_is_not_a_response() -> None:
    requested = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    old = (requested - timedelta(minutes=5)).isoformat()
    health = {
        "generation_id": "generation",
        "manager_state": "stopping",
        "manager_started_at": old,
        "generated_at": old,
    }
    assert not _is_fresh_response(
        health,
        {"generation_id": "generation"},
        previous_started=None,
        requested_at=requested,
    )
