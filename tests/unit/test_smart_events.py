import json
from datetime import UTC, datetime

from backup_system.common.events import SmartObserved, parse_executor_event
from backup_system.common.smart import SmartMetrics
from backup_system.executor.smart_events import build_smart_events
from backup_system.executor.smart_preflight import SmartPreflightObservation


def test_smart_event_contains_normalized_metrics_without_hardware_identity() -> None:
    observation = SmartPreflightObservation(
        disk_id="source-main",
        collection_success=True,
        health="healthy",
        metrics=SmartMetrics(pending_sectors=0),
        identity_key="a" * 64,
    )
    event = build_smart_events((observation,), timestamp=datetime.now(UTC))[0]
    serialized = event.model_dump_json()
    payload = json.loads(serialized)
    assert payload["disk_id"] == "source-main"
    assert payload["identity_key"] == "a" * 64
    assert "serial" not in serialized.casefold()
    assert "device" not in serialized.casefold()
    assert isinstance(parse_executor_event(payload), SmartObserved)


def test_unknown_collection_has_no_identity_baseline() -> None:
    observation = SmartPreflightObservation(
        "source-main",
        False,
        "unknown",
        SmartMetrics(),
        "smartctl scan failed",
    )
    event = build_smart_events((observation,), timestamp=datetime.now(UTC))[0]
    assert event.identity_key is None
    assert not event.collection_success
