"""Bounded passive smartctl JSON collection for the configured disk allowlist."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from backup_system.common.config import SmartConfig, SmartDiskConfig
from backup_system.common.smart import SmartMetrics


class SmartctlError(RuntimeError):
    pass


class SmartctlBackend(Protocol):
    def read(self, device: str, *, timeout_seconds: int) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SmartPreflightObservation:
    disk_id: str
    collection_success: bool
    health: Literal["healthy", "warning", "critical", "unknown"]
    metrics: SmartMetrics
    reason: str | None = None
    identity_key: str | None = None


class SubprocessSmartctlBackend:
    def __init__(self, executable: Path) -> None:
        self._executable = executable

    def read(self, device: str, *, timeout_seconds: int) -> dict[str, Any]:
        return self._run(("--all", "--json=o", device), timeout_seconds=timeout_seconds)

    def _run(self, arguments: tuple[str, ...], *, timeout_seconds: int) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                (str(self._executable), *arguments),
                capture_output=True,
                check=False,
                shell=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SmartctlError("smartctl execution failed or timed out") from error
        try:
            payload = json.loads(completed.stdout.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SmartctlError("smartctl returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise SmartctlError("smartctl returned a non-object JSON value")
        return payload


class SmartPreflight:
    def __init__(
        self,
        backend: SmartctlBackend,
        *,
        cancellation_checkpoint: Callable[[], None] | None = None,
    ) -> None:
        self._backend = backend
        self._cancellation_checkpoint = cancellation_checkpoint or (lambda: None)

    def collect(self, config: SmartConfig) -> tuple[SmartPreflightObservation, ...]:
        observations: list[SmartPreflightObservation] = []
        for configured in config.disks:
            self._cancellation_checkpoint()
            try:
                payload = self._backend.read(
                    configured.identity.device,
                    timeout_seconds=config.per_disk_timeout_seconds,
                )
            except SmartctlError:
                observations.append(_unknown(configured, "smartctl read failed"))
            else:
                self._cancellation_checkpoint()
                observations.append(self._observation(configured, payload))
        return tuple(observations)

    @staticmethod
    def _observation(
        configured: SmartDiskConfig, payload: dict[str, Any]
    ) -> SmartPreflightObservation:
        if _normalize(str(payload.get("serial_number", ""))) != _normalize(
            configured.identity.serial
        ):
            return _unknown(configured, "configured SMART disk serial does not match")
        capacity = _nested_int(payload, "user_capacity", "bytes")
        # Some ATA bridges omit user_capacity even though discovery has already
        # cross-checked the Windows inventory. Reject a contradictory value, but
        # keep a serial-matched observation when the field is absent.
        if capacity is not None and capacity != configured.identity.expected_size_bytes:
            return _unknown(configured, "configured SMART disk capacity does not match")
        metrics = _parse_metrics(payload)
        health: Literal["critical", "healthy"]
        if metrics.overall_passed is False or metrics.nvme_critical_warning is True:
            health = "critical"
        else:
            health = "healthy"
        return SmartPreflightObservation(
            configured.id,
            True,
            health,
            metrics,
            identity_key=_identity_key(configured),
        )


def _parse_metrics(payload: dict[str, Any]) -> SmartMetrics:
    attributes = _ata_attributes(payload)
    nvme = payload.get("nvme_smart_health_information_log")
    nvme = nvme if isinstance(nvme, dict) else {}
    return SmartMetrics(
        overall_passed=_nested_bool(payload, "smart_status", "passed"),
        nvme_critical_warning=_optional_bool(nvme.get("critical_warning")),
        temperature_celsius=_first_not_none(
            _nested_int(payload, "temperature", "current"),
            _optional_int(nvme.get("temperature")),
        ),
        power_on_hours=_first_not_none(
            _nested_int(payload, "power_on_time", "hours"),
            _optional_int(nvme.get("power_on_hours")),
        ),
        reallocated_sectors=attributes.get(5),
        pending_sectors=attributes.get(197),
        offline_uncorrectable=attributes.get(198),
        reported_uncorrectable=attributes.get(187),
        interface_crc_errors=attributes.get(199),
        nvme_percentage_used=_optional_int(nvme.get("percentage_used")),
        nvme_media_errors=_optional_int(nvme.get("media_errors")),
    )


def _ata_attributes(payload: dict[str, Any]) -> dict[int, int]:
    table = payload.get("ata_smart_attributes")
    table = table.get("table") if isinstance(table, dict) else None
    result: dict[int, int] = {}
    if not isinstance(table, list):
        return result
    for item in table:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        raw = item.get("raw")
        value = raw.get("value") if isinstance(raw, dict) else None
        if isinstance(value, int) and value >= 0:
            result[int(item["id"])] = value
    return result


def _unknown(configured: SmartDiskConfig, reason: str) -> SmartPreflightObservation:
    return SmartPreflightObservation(configured.id, False, "unknown", SmartMetrics(), reason)


def _nested_int(payload: dict[str, Any], outer: str, inner: str) -> int | None:
    value = payload.get(outer)
    return _optional_int(value.get(inner)) if isinstance(value, dict) else None


def _nested_bool(payload: dict[str, Any], outer: str, inner: str) -> bool | None:
    value = payload.get(outer)
    return _optional_bool(value.get(inner)) if isinstance(value, dict) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return None


def _normalize(value: str) -> str:
    return value.replace("\x00", "").strip().casefold()


def _first_not_none(first: int | None, second: int | None) -> int | None:
    return first if first is not None else second


def _identity_key(configured: SmartDiskConfig) -> str:
    material = (
        _normalize(configured.identity.serial) + "\0" + str(configured.identity.expected_size_bytes)
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()
