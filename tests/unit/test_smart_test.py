from pathlib import Path

import pytest

from backup_system.common.config import SmartDiskConfig
from backup_system.executor.smart_test import (
    SmartSelfTestError,
    SmartSelfTestStatus,
    SubprocessSmartSelfTestBackend,
    parse_self_test_status,
    run_smart_self_test,
    run_smart_self_tests,
)
from backup_system.executor.storage_inventory import DiskRecord


class Backend:
    def __init__(self, statuses: list[SmartSelfTestStatus]) -> None:
        self.statuses = statuses
        self.started: tuple[str, str] | None = None

    def identify(self, device: str) -> tuple[str, int | None]:
        assert device == "/dev/pd2"
        return "TEST-SERIAL", 1_000_000

    def start(self, device: str, test_type: str) -> None:
        self.started = (device, test_type)

    def status(self, device: str) -> SmartSelfTestStatus:
        assert device == "/dev/pd2"
        return self.statuses.pop(0)


def _disk() -> SmartDiskConfig:
    return SmartDiskConfig.model_validate(
        {
            "id": "test-disk",
            "display_name": "Test disk",
            "identity": {
                "device": "/dev/pd2",
                "serial": "test-serial",
                "expected_size_bytes": 1_000_000,
            },
        }
    )


def test_self_test_identifies_starts_polls_and_completes() -> None:
    backend = Backend(
        [
            SmartSelfTestStatus(True, None, "in progress", 90),
            SmartSelfTestStatus(False, True, "Completed without error"),
        ]
    )
    clock = iter((0.0, 1.0))
    checkpoints: list[None] = []
    result = run_smart_self_test(
        backend=backend,
        disk=_disk(),
        test_type="short",
        poll_seconds=10,
        timeout_seconds=100,
        checkpoint=lambda: checkpoints.append(None),
        sleep=lambda value: None,
        monotonic=lambda: next(clock),
    )
    assert backend.started == ("/dev/pd2", "short")
    assert result.passed is True
    assert len(checkpoints) == 3


def test_self_test_rejects_identity_mismatch_before_start() -> None:
    backend = Backend([])
    backend.identify = lambda device: ("OTHER", 1_000_000)  # type: ignore[method-assign]
    with pytest.raises(SmartSelfTestError, match="serial does not match"):
        run_smart_self_test(
            backend=backend,
            disk=_disk(),
            test_type="short",
            poll_seconds=1,
            timeout_seconds=10,
            checkpoint=lambda: None,
        )
    assert backend.started is None


def test_all_system_discovery_uses_windows_disk_numbers_and_verifies_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = SubprocessSmartSelfTestBackend(
        tmp_path / "smartctl.exe",
        inventory=lambda: (
            DiskRecord(3, "SERIAL-3", 3000, False, False, False),
            DiskRecord(0, "SERIAL-0", 1000, False, True, True),
        ),
    )
    identities = {
        "/dev/pd0": ("SERIAL-0", 1000),
        "/dev/pd3": ("SERIAL-3", 3000),
    }
    monkeypatch.setattr(backend, "identify", lambda device: identities[device])

    disks = backend.discover()

    assert [disk.identity.device for disk in disks] == ["/dev/pd0", "/dev/pd3"]
    assert [disk.identity.serial for disk in disks] == ["SERIAL-0", "SERIAL-3"]


def test_multi_disk_test_continues_after_one_disk_fails() -> None:
    class MultiBackend(Backend):
        def identify(self, device: str) -> tuple[str, int | None]:
            return ("wrong" if device == "/dev/pd2" else "test-serial"), 1_000_000

        def status(self, device: str) -> SmartSelfTestStatus:
            return self.statuses.pop(0)

    backend = MultiBackend([SmartSelfTestStatus(False, True, "Completed")])
    second = _disk().model_copy(
        update={"identity": _disk().identity.model_copy(update={"device": "/dev/pd3"})}
    )
    observed: list[tuple[int, int]] = []
    results = run_smart_self_tests(
        backend=backend,
        disks=(_disk(), second),
        test_type="short",
        poll_seconds=1,
        timeout_seconds=10,
        checkpoint=lambda: None,
        on_disk=lambda index, total: observed.append((index, total)),
    )
    assert [result.result for result in results] == ["failed", "success"]
    assert results[0].reason == "configured SMART disk serial does not match"
    assert observed == [(1, 2), (2, 2)]


def test_ata_and_nvme_status_are_classified() -> None:
    ata = parse_self_test_status(
        {
            "ata_smart_data": {
                "self_test": {
                    "status": {"value": 249, "string": "in progress", "remaining_percent": 90}
                }
            }
        }
    )
    assert ata.running and ata.remaining_percent == 90
    nvme = parse_self_test_status(
        {
            "nvme_self_test_log": {
                "current_self_test_operation": {"value": 0},
                "table": [{"self_test_result": {"value": 0, "string": "Completed"}}],
            }
        }
    )
    assert not nvme.running and nvme.passed is True
