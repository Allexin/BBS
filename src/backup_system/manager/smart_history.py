"""Persistent SMART observations and comparison with the previous valid baseline."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from backup_system.common.smart import SmartMetrics
from backup_system.common.time import require_aware
from backup_system.manager.notifications import NotificationRepository


class SmartSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


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
    def __init__(
        self,
        connection: sqlite3.Connection,
        notifications: NotificationRepository | None = None,
    ) -> None:
        self._connection = connection
        self._notifications = notifications

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

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
        manufacturer: str | None = None,
        mount_points: tuple[str, ...] = (),
    ) -> SmartComparison:
        timestamp = require_aware(observed_at).isoformat()
        with self._connection:
            disk_id = self._merge_identity_rows(disk_id, identity_key)
        public_disk_id = f"disk-{identity_key[:12]}"
        previous = self._previous_successful(disk_id)
        comparable = previous is not None and previous[0] == identity_key
        previous_metrics = previous[1] if previous is not None and comparable else None
        regressions, resets = self._compare(previous_metrics, metrics)
        new_critical_conditions = (
            _absolute_critical_conditions(metrics)
            - _absolute_critical_conditions(previous_metrics)
            if collection_success
            else set()
        )
        new_critical_conditions.difference_update(item.rule_id for item in regressions)
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
                    capacity_bytes, role, last_seen_at, manufacturer, mount_points_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(disk_id) DO UPDATE SET
                    public_disk_id = excluded.public_disk_id,
                    model = excluded.model,
                    media_type = excluded.media_type,
                    bus_type = excluded.bus_type,
                    capacity_bytes = excluded.capacity_bytes,
                    role = excluded.role,
                    last_seen_at = excluded.last_seen_at,
                    manufacturer = excluded.manufacturer,
                    mount_points_json = excluded.mount_points_json""",
                (
                    disk_id,
                    public_disk_id,
                    model,
                    media_type,
                    bus_type,
                    capacity_bytes,
                    role,
                    timestamp,
                    manufacturer,
                    json.dumps(mount_points, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self._connection.execute(
                """UPDATE smart_test_results SET disk_id = ?
                WHERE disk_id != ? AND identity_key = ?""",
                (disk_id, disk_id, identity_key),
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
            if self._notifications is not None:
                for regression in regressions if collection_success else ():
                    self._notifications.enqueue_in_transaction(
                        deduplication_key=(f"smart:{observation_id}:{regression.rule_id}"),
                        kind="smart_regression",
                        payload={
                            "disk": public_disk_id,
                            "indicator": regression.rule_id,
                            "previous": regression.previous,
                            "current": regression.current,
                            "severity": regression.severity,
                        },
                        created_at=observed_at,
                    )
                for condition in sorted(new_critical_conditions):
                    self._notifications.enqueue_in_transaction(
                        deduplication_key=f"smart:{observation_id}:critical:{condition}",
                        kind="smart_critical_condition",
                        payload={
                            "disk": public_disk_id,
                            "indicator": condition,
                            "current": _condition_value(metrics, condition),
                            "severity": SmartSeverity.CRITICAL,
                        },
                        created_at=observed_at,
                    )
        return SmartComparison(
            observation_id=observation_id,
            baseline_created=collection_success and not comparable,
            regressions=regressions if collection_success else (),
            reset_counters=resets if collection_success else (),
        )

    def _merge_identity_rows(self, requested_disk_id: str, identity_key: str) -> str:
        matches: list[str] = []
        for candidate, normalized_json in self._connection.execute(
            """SELECT physical_disks.disk_id, disk_observations.normalized_json
            FROM physical_disks JOIN disk_observations ON disk_observations.observation_id = (
                SELECT observation_id FROM disk_observations latest
                WHERE latest.disk_id = physical_disks.disk_id
                ORDER BY observation_id DESC LIMIT 1)
            ORDER BY physical_disks.rowid"""
        ):
            if json.loads(str(normalized_json)).get("identity_key") == identity_key:
                matches.append(str(candidate))
        canonical = matches[0] if matches else requested_disk_id
        return canonical

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


def _absolute_critical_conditions(metrics: SmartMetrics | None) -> set[str]:
    if metrics is None:
        return set()
    conditions: set[str] = set()
    if metrics.overall_passed is False:
        conditions.add("smart_overall_failed")
    if metrics.nvme_critical_warning is True:
        conditions.add("nvme_critical_warning")
    for field in (
        "pending_sectors",
        "offline_uncorrectable",
        "reported_uncorrectable",
        "nvme_media_errors",
    ):
        value = getattr(metrics, field)
        if value is not None and value > 0:
            conditions.add(field)
    return conditions


def _condition_value(metrics: SmartMetrics, condition: str) -> int | bool | None:
    fields = {
        "smart_overall_failed": "overall_passed",
        "nvme_critical_warning": "nvme_critical_warning",
    }
    return getattr(metrics, fields.get(condition, condition), None)
