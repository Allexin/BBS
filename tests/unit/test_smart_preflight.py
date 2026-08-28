from typing import Any

import pytest

from backup_system.common.config import SmartConfig
from backup_system.executor.cancellation import CancellationRequested, CancellationToken
from backup_system.executor.smart_preflight import SmartctlError, SmartPreflight


def _config() -> SmartConfig:
    return SmartConfig.model_validate(
        {
            "per_disk_timeout_seconds": 5,
            "stale_after_hours": 24,
            "disks": [
                {
                    "id": "backup-disk",
                    "display_name": "Backup disk",
                    "identity": {"serial": "SERIAL-1", "expected_size_bytes": 1000},
                }
            ],
        }
    )


class FakeSmartctl:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.timeout: int | None = None
        self.scan_error = False
        self.devices = (r"/dev/pd0",)
        self.read_devices: list[str] = []
        self.after_read: object = None

    def scan(self) -> tuple[str, ...]:
        if self.scan_error:
            raise SmartctlError("scan")
        return self.devices

    def read(self, device: str, *, timeout_seconds: int) -> dict[str, Any]:
        self.timeout = timeout_seconds
        self.read_devices.append(device)
        if callable(self.after_read):
            self.after_read()
        return self.payload


def test_ata_smart_json_is_normalized_without_absolute_threshold_gaps() -> None:
    payload = {
        "serial_number": " serial-1\x00 ",
        "user_capacity": {"bytes": 1000},
        "smart_status": {"passed": True},
        "temperature": {"current": 35},
        "power_on_time": {"hours": 500},
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "raw": {"value": 2}},
                {"id": 197, "raw": {"value": 1}},
                {"id": 198, "raw": {"value": 0}},
                {"id": 199, "raw": {"value": 4}},
            ]
        },
    }
    backend = FakeSmartctl(payload)
    observation = SmartPreflight(backend).collect(_config())[0]
    assert observation.collection_success and observation.health == "healthy"
    assert observation.metrics.pending_sectors == 1
    assert observation.metrics.interface_crc_errors == 4
    assert observation.identity_key is not None
    assert len(observation.identity_key) == 64
    assert "serial-1" not in observation.identity_key
    assert backend.timeout == 5


def test_nvme_critical_warning_is_critical() -> None:
    backend = FakeSmartctl(
        {
            "serial_number": "SERIAL-1",
            "user_capacity": {"bytes": 1000},
            "smart_status": {"passed": True},
            "nvme_smart_health_information_log": {
                "critical_warning": 1,
                "percentage_used": 8,
                "media_errors": 2,
            },
        }
    )
    observation = SmartPreflight(backend).collect(_config())[0]
    assert observation.health == "critical"
    assert observation.metrics.nvme_critical_warning is True


def test_missing_mismatched_or_failed_scan_is_unknown_not_exception() -> None:
    mismatch = FakeSmartctl({"serial_number": "OTHER", "user_capacity": {"bytes": 1000}})
    observation = SmartPreflight(mismatch).collect(_config())[0]
    assert not observation.collection_success and observation.health == "unknown"

    failed = FakeSmartctl({})
    failed.scan_error = True
    observation = SmartPreflight(failed).collect(_config())[0]
    assert not observation.collection_success and observation.reason == "smartctl scan failed"


def test_cancellation_stops_allowlist_before_next_device_read() -> None:
    token = CancellationToken()
    backend = FakeSmartctl({})
    backend.devices = (r"/dev/pd0", r"/dev/pd1")
    backend.after_read = token.request
    preflight = SmartPreflight(
        backend,
        cancellation_checkpoint=token.raise_if_requested,
    )

    with pytest.raises(CancellationRequested):
        preflight.collect(_config())

    assert backend.read_devices == [r"/dev/pd0"]
