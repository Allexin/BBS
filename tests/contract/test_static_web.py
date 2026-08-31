from pathlib import Path

ROOT = Path(__file__).parents[2]
WEB = ROOT / "web"


def test_status_and_logs_ui_are_static_read_only_assets() -> None:
    expected = {"index.html", "logs.html", "styles.css", "status.js", "logs.js"}
    assert expected <= {path.name for path in WEB.iterdir() if path.is_file()}
    combined = "\n".join((WEB / name).read_text(encoding="utf-8") for name in expected)
    lowered = combined.lower()
    assert "fetch(" in combined
    assert "method:" not in lowered
    assert "post" not in lowered
    assert "websocket" not in lowered
    assert "eventsource" not in lowered
    assert ".innerhtml" not in lowered


def test_logs_ui_fetches_day_only_on_open_or_explicit_refresh() -> None:
    script = (WEB / "logs.js").read_text(encoding="utf-8")
    assert "setInterval(fetchIndex" in script
    assert "setInterval(loadSelected" not in script
    assert 'byId("refresh-log").addEventListener("click"' in script


def test_nginx_example_has_only_scoped_static_aliases() -> None:
    config = (ROOT / "docs" / "nginx-readonly.example.conf").read_text(encoding="utf-8")
    lowered = config.lower()
    assert "limit_except get" in lowered
    assert "proxy_pass" not in lowered
    assert "data/public" in config
    assert "Stable/web" in config
    assert "Stable/data/config" not in config
    assert "Stable/data/state" not in config


def test_status_ui_labels_missing_disk_identity_fields() -> None:
    script = (WEB / "status.js").read_text(encoding="utf-8")
    assert 'value ?? "not reported"' not in script
    assert "Manufacturer: ${dash(disk.manufacturer)}" in script
    assert "Capacity: ${formatBytes(disk.capacity_bytes)}" in script


def test_status_ui_shows_active_job_heartbeat_in_job_card() -> None:
    script = (WEB / "status.js").read_text(encoding="utf-8")
    assert 'addFact(facts,"Current status",operationStatus(current))' in script
    assert "heartbeat ${age(progress.updated_at)}" in script
    assert "status heartbeat is stale" in script
    assert "projectionAge>health.status_stale_after_seconds" in script
    assert 'text("details",null,"card disk-card")' in script
    assert 'addFact(facts,"Last duration"' in script
    assert 'addFact(facts,"Repository size"' in script
