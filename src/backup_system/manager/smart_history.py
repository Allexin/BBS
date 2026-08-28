"""Persistent SMART observations and comparison with the previous valid baseline."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from backup_system.common.time import require_aware


class SmartSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class SmartMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    overall_passed: bool | None = None
    nvme_critical_warning: bool | None = None
    temperature_celsius: int | None = None
    power_on_hours: int | None = Field(default=None, ge=0)
    reallocated_sectors: int | None = Field(default=None, ge=0)
    pending_sectors: int | None = Field(default=None, ge=0)
    offline_uncorrectable: int | None = Field(default=None, ge=0)
    reported_uncorrectable: int | None = Field(default=None, ge=0)
    interface_crc_errors: int | None = Field(default=None, ge=0)
    nvme_percentage_used: int | None = Field(default=None, ge=0)
    nvme_media_errors: int | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class SmartRegression:
    rule_id: str
    severity: SmartSeverity
    previous: int | bool
    current: int | bool


@dataclass(frozen=True, slots=True)
class SmartComparison:
    observation_id: int
    baseline_created: bool
    regressions: tuple[SmartRegression, ...]
    reset_counters: tuple[str, ...]


_COUNTER_RULES: tuple[tuple[str, SmartSeverity], ...] = (
    ("reallocated_sectors", SmartSeverity.WARNING),
    ("pending_sectors", SmartSeverity.CRITICAL),
    ("offline_uncorrectable", SmartSeverity.CRITICAL),
    ("reported_uncorrectable", SmartSeverity.CRITICAL),
    ("interface_crc_errors", SmartSeverity.WARNING),
    ("nvme_media_errors", SmartSeverity.CRITICAL),
)


class SmartHistoryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record(
        self,
        *,
        disk_id: str,
        public_disk_id: str,
        identity_key: str,
        role: str,
        observed_at: datetime,
        operational_state: str,
        smart_health: str,
        metrics: SmartMetrics,
        collection_success: bool = True,
        model: str | None = None,
        media_type: str | None = None,
        bus_type: str | None = None,
        capacity_bytes: int | None = None,
    ) -> SmartComparison:
        timestamp = require_aware(observed_at).isoformat()
        previous = self._previous_successful(disk_id)
        comparable = previous is not None and previous[0] == identity_key
        previous_metrics = previous[1] if previous is not None and comparable else None
        regressions, resets = self._compare(previous_metrics, metrics)
        normalized = {
            "schema_version": 1,
            "collection_success": collection_success,
            "identity_key": identity_key,
            "metrics": metrics.model_dump(mode="json"),
        }
        with self._connection:
            self._connection.execute(
                """INSERT INTO physical_disks(
                    disk_id, public_disk_id, model, media_type, bus_type,
                    capacity_bytes, role, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(disk_id) DO UPDATE SET
                    public_disk_id = excluded.public_disk_id,
                    model = excluded.model,
                    media_type = excluded.media_type,
                    bus_type = excluded.bus_type,
                    capacity_bytes = excluded.capacity_bytes,
                    role = excluded.role,
                    last_seen_at = excluded.last_seen_at""",
                (
                    disk_id,
                    public_disk_id,
                    model,
                    media_type,
                    bus_type,
                    capacity_bytes,
                    role,
                    timestamp,
                ),
            )
            cursor = self._connection.execute(
                """INSERT INTO disk_observations(
                    disk_id, observed_at, operational_state, smart_health,
                    temperature_celsius, power_on_hours, reallocated_sectors,
                    pending_sectors, offline_uncorrectable, interface_crc_errors,
                    nvme_percentage_used, nvme_media_errors, normalized_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    disk_id,
                    timestamp,
                    operational_state,
                    smart_health,
                    metrics.temperature_celsius,
                    metrics.power_on_hours,
                    metrics.reallocated_sectors,
                    metrics.pending_sectors,
                    metrics.offline_uncorrectable,
                    metrics.interface_crc_errors,
                    metrics.nvme_percentage_used,
                    metrics.nvme_media_errors,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an observation ID")
            observation_id = cursor.lastrowid
        return SmartComparison(
            observation_id=observation_id,
            baseline_created=collection_success and not comparable,
            regressions=regressions if collection_success else (),
            reset_counters=resets if collection_success else (),
        )

    def _previous_successful(self, disk_id: str) -> tuple[str, SmartMetrics] | None:
        rows = self._connection.execute(
            """SELECT normalized_json FROM disk_observations
            WHERE disk_id = ? ORDER BY observation_id DESC""",
            (disk_id,),
        )
        for row in rows:
            value = json.loads(str(row[0]))
            if value.get("collection_success") is True:
                return str(value["identity_key"]), SmartMetrics.model_validate(value["metrics"])
        return None

    @staticmethod
    def _compare(
        previous: SmartMetrics | None, current: SmartMetrics
    ) -> tuple[tuple[SmartRegression, ...], tuple[str, ...]]:
        if previous is None:
            return (), ()
        regressions: list[SmartRegression] = []
        resets: list[str] = []
        if previous.overall_passed is True and current.overall_passed is False:
            regressions.append(
                SmartRegression("smart_overall_failed", SmartSeverity.CRITICAL, True, False)
            )
        if previous.nvme_critical_warning is False and current.nvme_critical_warning is True:
            regressions.append(
                SmartRegression("nvme_critical_warning", SmartSeverity.CRITICAL, False, True)
            )
        for field_name, severity in _COUNTER_RULES:
            old_value = getattr(previous, field_name)
            new_value = getattr(current, field_name)
            if old_value is None or new_value is None or old_value == new_value:
                continue
            if new_value > old_value:
                regressions.append(SmartRegression(field_name, severity, old_value, new_value))
            else:
                resets.append(field_name)
        return tuple(regressions), tuple(resets)
