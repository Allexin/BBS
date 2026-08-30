import pytest

from backup_system.common.config import SmartDiskConfig
from backup_system.executor.smart_test import (
    SmartSelfTestError,
    SmartSelfTestStatus,
    parse_self_test_status,
    run_smart_self_test,
)


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
