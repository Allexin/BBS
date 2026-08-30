"""Scheduled SMART self-test execution without storage topology changes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from backup_system.common.config import SmartDiskConfig
from backup_system.executor.storage_inventory import ComStorageInventorySource, DiskRecord


class SmartSelfTestError(RuntimeError):
    def __init__(self, message: str, *, remaining_percent: int | None = None) -> None:
        super().__init__(message)
        self.remaining_percent = remaining_percent


@dataclass(frozen=True, slots=True)
class SmartSelfTestStatus:
    running: bool
    passed: bool | None
    description: str
    remaining_percent: int | None = None


@dataclass(frozen=True, slots=True)
class SmartSelfTestResult:
    disk: SmartDiskConfig
    result: Literal["success", "failed", "timeout", "unsupported"]
    reason: str
    duration_seconds: int
    remaining_percent: int | None

    @property
    def identity_key(self) -> str:
        material = (
            _normalize(self.disk.identity.serial)
            + "\0"
            + str(self.disk.identity.expected_size_bytes)
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()


class SmartSelfTestBackend(Protocol):
    def discover(self) -> tuple[SmartDiskConfig, ...]: ...

    def identify(self, device: str) -> tuple[str, int | None]: ...

    def start(self, device: str, test_type: Literal["short", "long"]) -> None: ...

    def status(self, device: str) -> SmartSelfTestStatus: ...


class SubprocessSmartSelfTestBackend:
    def __init__(
        self,
        executable: Path,
        *,
        command_timeout_seconds: int = 30,
        inventory: Callable[[], Sequence[DiskRecord]] | None = None,
    ) -> None:
        self._executable = executable
        self._timeout = command_timeout_seconds
        self._inventory = inventory or (lambda: ComStorageInventorySource().disks())
        self._capacity_fallbacks: dict[str, int] = {}

    def identify(self, device: str) -> tuple[str, int | None]:
        info = self._run(("--all", "--json=o", device))
        return str(info.get("serial_number", "")), (
            _capacity(info) or self._capacity_fallbacks.get(device)
        )

    def discover(self) -> tuple[SmartDiskConfig, ...]:
        discovered: list[SmartDiskConfig] = []
        records = sorted(self._inventory(), key=lambda item: item.number)
        for index, record in enumerate(records):
            device = f"/dev/pd{record.number}"
            serial, capacity = self.identify(device)
            if not serial:
                raise SmartSelfTestError("discovered SMART disk identity is incomplete")
            if _normalize(serial) != _normalize(record.serial):
                raise SmartSelfTestError("Windows and smartctl disk serials do not match")
            if capacity is not None and capacity != record.size_bytes:
                raise SmartSelfTestError("Windows and smartctl disk capacities do not match")
            self._capacity_fallbacks[device] = record.size_bytes
            capacity = record.size_bytes
            discovered.append(
                SmartDiskConfig.model_validate(
                    {
                        "id": f"system-disk-{index + 1}",
                        "display_name": f"System disk {index + 1}",
                        "identity": {
                            "device": device,
                            "serial": serial,
                            "expected_size_bytes": capacity,
                        },
                    }
                )
            )
        if not discovered:
            raise SmartSelfTestError("smartctl discovery found no physical disks")
        return tuple(discovered)

    def start(self, device: str, test_type: Literal["short", "long"]) -> None:
        payload = self._run((f"--test={test_type}", "--json=o", device))
        messages = payload.get("messages")
        if isinstance(messages, list) and any(
            isinstance(item, dict) and str(item.get("severity", "")).casefold() == "error"
            for item in messages
        ):
            raise SmartSelfTestError("smartctl rejected self-test start")

    def status(self, device: str) -> SmartSelfTestStatus:
        return parse_self_test_status(
            self._run(("--capabilities", "--log=selftest", "--json=o", device))
        )

    def _run(self, arguments: tuple[str, ...]) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                (str(self._executable), *arguments),
                capture_output=True,
                check=False,
                shell=False,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SmartSelfTestError("smartctl command failed or timed out") from error
        try:
            value = json.loads(completed.stdout.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SmartSelfTestError("smartctl returned invalid JSON") from error
        if not isinstance(value, dict):
            raise SmartSelfTestError("smartctl returned a non-object JSON value")
        return value


def run_smart_self_test(
    *,
    backend: SmartSelfTestBackend,
    disk: SmartDiskConfig,
    test_type: Literal["short", "long"],
    poll_seconds: int,
    timeout_seconds: int,
    checkpoint: Callable[[], None],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SmartSelfTestStatus:
    device = disk.identity.device
    serial, capacity = backend.identify(device)
    if _normalize(serial) != _normalize(disk.identity.serial):
        raise SmartSelfTestError("configured SMART disk serial does not match")
    if capacity != disk.identity.expected_size_bytes:
        raise SmartSelfTestError("configured SMART disk capacity does not match")
    checkpoint()
    backend.start(device, test_type)
    deadline = monotonic() + timeout_seconds
    while True:
        checkpoint()
        status = backend.status(device)
        if not status.running:
            if status.passed is not True:
                raise SmartSelfTestError("SMART self-test did not complete successfully")
            return status
        if monotonic() >= deadline:
            raise SmartSelfTestError(
                "SMART self-test completion timed out",
                remaining_percent=status.remaining_percent,
            )
        sleep(poll_seconds)


def run_smart_self_tests(
    *,
    backend: SmartSelfTestBackend,
    disks: tuple[SmartDiskConfig, ...],
    test_type: Literal["short", "long"],
    poll_seconds: int,
    timeout_seconds: int,
    checkpoint: Callable[[], None],
    on_disk: Callable[[int, int], None],
    on_result: Callable[[SmartSelfTestResult], None] = lambda result: None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[SmartSelfTestResult, ...]:
    results: list[SmartSelfTestResult] = []
    for index, disk in enumerate(disks, start=1):
        on_disk(index, len(disks))
        started = monotonic()
        try:
            status = run_smart_self_test(
                backend=backend,
                disk=disk,
                test_type=test_type,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
                checkpoint=checkpoint,
            )
            result = SmartSelfTestResult(
                disk=disk,
                result="success",
                reason=status.description,
                duration_seconds=int(monotonic() - started),
                remaining_percent=status.remaining_percent,
            )
        except SmartSelfTestError as error:
            reason = str(error)
            outcome: Literal["failed", "timeout", "unsupported"] = (
                "timeout"
                if "timed out" in reason
                else "unsupported"
                if "unsupported" in reason
                else "failed"
            )
            result = SmartSelfTestResult(
                disk=disk,
                result=outcome,
                reason=reason,
                duration_seconds=int(monotonic() - started),
                remaining_percent=error.remaining_percent,
            )
        results.append(result)
        on_result(result)
    return tuple(results)


def parse_self_test_status(payload: dict[str, Any]) -> SmartSelfTestStatus:
    ata = payload.get("ata_smart_data")
    self_test = ata.get("self_test") if isinstance(ata, dict) else None
    status = self_test.get("status") if isinstance(self_test, dict) else None
    if isinstance(status, dict):
        description = str(status.get("string", "unknown"))
        remaining = _nonnegative_int(status.get("remaining_percent"))
        value = _nonnegative_int(status.get("value"))
        if value is not None and value >> 4 == 0x0F:
            return SmartSelfTestStatus(True, None, description, remaining)
    else:
        description = "unknown"

    log = payload.get("ata_smart_self_test_log")
    standard = log.get("standard") if isinstance(log, dict) else None
    table = standard.get("table") if isinstance(standard, dict) else None
    if isinstance(table, list) and table and isinstance(table[0], dict):
        result = table[0].get("status")
        if isinstance(result, dict):
            description = str(result.get("string", description))
            passed = _nonnegative_int(result.get("value")) == 0
            return SmartSelfTestStatus(False, passed, description)
    nvme = payload.get("nvme_self_test_log")
    current = nvme.get("current_self_test_operation") if isinstance(nvme, dict) else None
    current_value = (
        _nonnegative_int(current.get("value")) if isinstance(current, dict) else None
    )
    if current_value not in {None, 0}:
        if not isinstance(nvme, dict):
            raise SmartSelfTestError("invalid NVMe self-test status")
        completion = nvme.get("current_self_test_completion_percent")
        completed = _nonnegative_int(completion)
        return SmartSelfTestStatus(
            True,
            None,
            "NVMe self-test in progress",
            100 - completed if completed is not None and completed <= 100 else None,
        )
    table = nvme.get("table") if isinstance(nvme, dict) else None
    if isinstance(table, list) and table and isinstance(table[0], dict):
        result = table[0].get("self_test_result")
        if isinstance(result, dict):
            value = _nonnegative_int(result.get("value"))
            return SmartSelfTestStatus(
                False,
                value == 0,
                str(result.get("string", "NVMe self-test result")),
            )
    raise SmartSelfTestError("smartctl returned no classifiable self-test status")


def _capacity(payload: dict[str, Any]) -> int | None:
    value = payload.get("user_capacity")
    return _nonnegative_int(value.get("bytes")) if isinstance(value, dict) else None


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _normalize(value: str) -> str:
    return value.replace("\x00", "").strip().casefold()
