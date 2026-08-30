"""Build the sanitized Status UI projection from manager state and validated config."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from backup_system.common.time import require_aware
from backup_system.manager.public_projection import (
    HealthProjection,
    PublicBackupMetrics,
    PublicDisk,
    PublicHealthIssue,
    PublicJob,
    PublicOperation,
    PublicProgress,
    PublicRun,
    PublicSmartMetric,
    PublicVolume,
    StatusProjection,
)

_SEVERITY = {"healthy": 0, "unknown": 1, "warning": 2, "critical": 3}
_REGRESSION_FIELDS = (
    "reallocated_sectors",
    "pending_sectors",
    "offline_uncorrectable",
    "reported_uncorrectable",
    "interface_crc_errors",
    "nvme_percentage_used",
    "nvme_media_errors",
)
_DISPLAY_SMART_FIELDS = (
    "overall_passed",
    "nvme_critical_warning",
    "temperature_celsius",
    "power_on_hours",
    *_REGRESSION_FIELDS,
)


class ProjectionBuilder:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        job_kinds: dict[str, Literal["snapshot", "mirror", "maintenance", "smart-test"]]
        | None = None,
        job_deadlines: dict[str, str | None] | None = None,
        next_operations: dict[str, str] | None = None,
        volume_stale_after_seconds: int = 120,
    ) -> None:
        self._connection = connection
        self._job_kinds = job_kinds or {}
        self._job_deadlines = job_deadlines or {}
        self._next_operations = next_operations or {}
        if volume_stale_after_seconds <= 0:
            raise ValueError("volume stale threshold must be positive")
        self._volume_stale_after = timedelta(seconds=volume_stale_after_seconds)

    def build(
        self,
        *,
        now: datetime,
        manager_started_at: datetime,
        manager_state: Literal["starting", "idle", "running", "stopping", "error"],
        version: str,
    ) -> tuple[StatusProjection, HealthProjection]:
        timestamp = _utc(now)
        generation_id = uuid4()
        jobs, job_issues = self._jobs(timestamp)
        disks, disk_issues = self._disks(timestamp)
        operations = self._operations(timestamp)
        volumes = self._volumes(timestamp)
        issues = (*job_issues, *disk_issues)
        overall = _overall_health(jobs, disks, issues)
        status = StatusProjection(
            generation_id=generation_id,
            generated_at=timestamp,
            overall_health=overall,
            backup_disk_state=_backup_disk_state(
                disks, bool(operations and operations[0].state == "running")
            ),
            operations=operations,
            jobs=jobs,
            disks=disks,
            volumes=volumes,
            health_issues=issues,
        )
        health = HealthProjection(
            generation_id=generation_id,
            generated_at=timestamp,
            manager_state=manager_state,
            manager_started_at=_utc(manager_started_at),
            version=version,
        )
        return status, health

    def _operations(self, now: datetime) -> tuple[PublicOperation, ...]:
        rows = self._connection.execute(
            """SELECT operations.operation_id, jobs.display_name, operations.kind,
                operations.trigger_source, operations.state, operations.queued_at,
                runs.stage, runs.started_at, runs.progress_updated_at,
                runs.files_done, runs.files_total, runs.bytes_done, runs.bytes_total
            FROM operations JOIN jobs ON jobs.job_id = operations.job_id
            LEFT JOIN runs ON runs.operation_id = operations.operation_id
                AND runs.state = 'running'
            WHERE operations.state IN ('running', 'queued')
            ORDER BY CASE operations.state WHEN 'running' THEN 0 ELSE 1 END,
                operations.queued_at, operations.rowid"""
        ).fetchall()
        result: list[PublicOperation] = []
        for position, row in enumerate(rows):
            running = str(row[4]) == "running"
            since = datetime.fromisoformat(str(row[7] if running else row[5]))
            progress = None
            if running and any(value is not None for value in row[9:13]):
                progress = PublicProgress(
                    files_done=row[9],
                    files_total=row[10],
                    bytes_done=row[11],
                    bytes_total=row[12],
                    updated_at=datetime.fromisoformat(str(row[8])) if row[8] else None,
                )
            result.append(
                PublicOperation(
                    operation_id=UUID(str(row[0])),
                    position=position,
                    job_display_name=str(row[1]),
                    kind=str(row[2]),
                    trigger_source=cast(Literal["scheduled", "manual"], str(row[3])),
                    state="running" if running else "queued",
                    stage=str(row[6]) if row[6] is not None else None,
                    elapsed_seconds=max(0, int((now - since).total_seconds())),
                    executor_state="running" if running else "not_started",
                    progress=progress,
                )
            )
        return tuple(result)

    def _jobs(self, now: datetime) -> tuple[tuple[PublicJob, ...], tuple[PublicHealthIssue, ...]]:
        rows = self._connection.execute(
            """SELECT jobs.job_id, jobs.display_name, jobs.config_valid,
                jobs.config_error, schedule_state.next_fire_at
            FROM jobs LEFT JOIN schedule_state ON schedule_state.job_id = jobs.job_id
            WHERE jobs.enabled = 1 ORDER BY jobs.job_id"""
        ).fetchall()
        jobs: list[PublicJob] = []
        issues: list[PublicHealthIssue] = []
        for job_id_value, display_name, config_valid, config_error, next_fire_at in rows:
            job_id = str(job_id_value)
            run_rows = self._connection.execute(
                """SELECT operations.state, runs.result, operations.queued_at,
                    runs.started_at, runs.finished_at, runs.deadline_exceeded_at,
                    runs.run_id, runs.stage
                FROM operations LEFT JOIN runs ON runs.operation_id = operations.operation_id
                WHERE operations.job_id = ?
                    AND operations.state IN ('queued', 'running', 'completed')
                ORDER BY operations.queued_at DESC, operations.rowid DESC LIMIT 2""",
                (job_id,),
            ).fetchall()
            public_runs = tuple(self._public_run(row, now) for row in run_rows)
            last_run = public_runs[0] if public_runs else None
            health_run = next((item for item in public_runs if item.state == "finished"), None)
            health: Literal["healthy", "warning", "critical", "unknown"] = "unknown"
            reason = "No runs recorded"
            if not bool(config_valid):
                health, reason = "critical", "Configuration is invalid"
            elif health_run is not None and health_run.result == "failed":
                health, reason = "warning", "Latest run failed"
            elif health_run is not None and health_run.deadline_exceeded:
                health, reason = "warning", "Latest run exceeded deadline"
            elif health_run is not None and health_run.result in {"success", "warning"}:
                health = "healthy" if health_run.result == "success" else "warning"
                reason = "Latest run completed" if health == "healthy" else "Latest run warned"
            if health in {"warning", "critical"}:
                issues.append(
                    PublicHealthIssue(
                        severity=cast(Literal["warning", "critical"], health),
                        kind="job",
                        subject=job_id,
                        summary=reason,
                    )
                )
            success_row = self._connection.execute(
                """SELECT finished_at FROM runs WHERE job_id = ?
                AND result IN ('success', 'warning') ORDER BY finished_at DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
            metrics_row = self._connection.execute(
                """SELECT backup_metrics.run_id FROM backup_metrics
                JOIN runs ON runs.run_id = backup_metrics.run_id
                WHERE runs.job_id = ? ORDER BY backup_metrics.observed_at DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
            metrics = self._latest_metrics(str(metrics_row[0])) if metrics_row else None
            jobs.append(
                PublicJob(
                    job_id=job_id,
                    display_name=str(display_name),
                    kind=self._job_kinds.get(job_id, "unknown"),
                    health=health,
                    health_reason=(
                        f"{reason}: {config_error}" if not config_valid and config_error else reason
                    ),
                    next_fire_at=(
                        datetime.fromisoformat(str(next_fire_at)) if next_fire_at else None
                    ),
                    next_operation=self._next_operations.get(job_id),
                    deadline=self._job_deadlines.get(job_id),
                    last_run=last_run,
                    previous_run=public_runs[1] if len(public_runs) > 1 else None,
                    last_success_at=(
                        datetime.fromisoformat(str(success_row[0])) if success_row else None
                    ),
                    backup_metrics=metrics,
                )
            )
        return tuple(jobs), tuple(issues)

    @staticmethod
    def _public_run(row: tuple[Any, ...], now: datetime) -> PublicRun:
        operation_state = str(row[0])
        queued = datetime.fromisoformat(str(row[2]))
        started = datetime.fromisoformat(str(row[3])) if row[3] else None
        finished = datetime.fromisoformat(str(row[4])) if row[4] else None
        state: Literal["queued", "running", "finished"]
        if operation_state == "queued":
            state = "queued"
        elif operation_state == "running":
            state = "running"
        else:
            state = "finished"
        return PublicRun(
            state=state,
            result=(
                cast(
                    Literal["success", "warning", "failed", "cancelled", "interrupted"],
                    str(row[1]),
                )
                if row[1] is not None
                else None
            ),
            started_at=started,
            finished_at=finished,
            duration_seconds=max(0, int(((finished or now) - (started or queued)).total_seconds())),
            deadline_exceeded=row[5] is not None,
            stage=str(row[7]) if len(row) > 7 and row[7] is not None else None,
        )

    def _latest_metrics(self, run_id: str) -> PublicBackupMetrics | None:
        row = self._connection.execute(
            """SELECT source_logical_bytes, protected_logical_bytes,
                retained_logical_bytes, bytes_read, bytes_written,
                repository_added_bytes, repository_physical_bytes, repository_free_bytes
            FROM backup_metrics WHERE run_id = ?""",
            (run_id,),
        ).fetchone()
        return (
            PublicBackupMetrics.model_validate(
                dict(zip(PublicBackupMetrics.model_fields, row, strict=True))
            )
            if row
            else None
        )

    def _disks(self, now: datetime) -> tuple[tuple[PublicDisk, ...], tuple[PublicHealthIssue, ...]]:
        disks: list[PublicDisk] = []
        issues: list[PublicHealthIssue] = []
        rows = self._connection.execute(
            """SELECT disk_id, public_disk_id, model, media_type, bus_type,
                capacity_bytes, role FROM physical_disks ORDER BY public_disk_id"""
        ).fetchall()
        for row in rows:
            observations = self._successful_observations(str(row[0]))
            latest_row = self._connection.execute(
                """SELECT observed_at, operational_state, smart_health FROM disk_observations
                WHERE disk_id = ? ORDER BY observation_id DESC LIMIT 1""",
                (str(row[0]),),
            ).fetchone()
            metrics = self._smart_metrics(observations, now)
            operational = _operational(str(latest_row[1])) if latest_row else "unknown"
            smart_health = _smart_health(str(latest_row[2])) if latest_row else "unknown"
            public_id = str(row[1])
            if smart_health in {"warning", "critical"}:
                issues.append(
                    PublicHealthIssue(
                        severity=cast(Literal["warning", "critical"], smart_health),
                        kind="smart",
                        subject=public_id,
                        summary=f"SMART health is {smart_health}",
                    )
                )
            disks.append(
                PublicDisk(
                    disk_id=public_id,
                    model=str(row[2]) if row[2] is not None else None,
                    media_type=_media_type(str(row[3]) if row[3] is not None else None),
                    bus_type=str(row[4]) if row[4] is not None else None,
                    capacity_bytes=row[5],
                    role=str(row[6]),
                    operational_state=operational,
                    smart_health=smart_health,
                    observed_at=datetime.fromisoformat(str(latest_row[0])) if latest_row else None,
                    metrics=metrics,
                )
            )
        return tuple(disks), tuple(issues)

    def _successful_observations(self, disk_id: str) -> list[tuple[datetime, dict[str, Any]]]:
        values: list[tuple[datetime, dict[str, Any]]] = []
        identity: str | None = None
        rows = self._connection.execute(
            """SELECT observed_at, normalized_json FROM disk_observations
            WHERE disk_id = ? ORDER BY observation_id DESC""",
            (disk_id,),
        ).fetchall()
        for observed_at, normalized_json in rows:
            normalized = json.loads(str(normalized_json))
            if normalized.get("collection_success") is not True:
                continue
            current_identity = str(normalized.get("identity_key"))
            if identity is None:
                identity = current_identity
            if current_identity != identity:
                continue
            metrics = normalized.get("metrics")
            if isinstance(metrics, dict):
                values.append((datetime.fromisoformat(str(observed_at)), metrics))
        return values

    @staticmethod
    def _smart_metrics(
        observations: list[tuple[datetime, dict[str, Any]]], now: datetime
    ) -> dict[str, PublicSmartMetric]:
        if not observations:
            return {}
        latest_time, latest = observations[0]
        previous = observations[1][1] if len(observations) > 1 else {}
        result: dict[str, PublicSmartMetric] = {}
        for field in _DISPLAY_SMART_FIELDS:
            current_value = latest.get(field)
            previous_value = previous.get(field)
            result[field] = PublicSmartMetric(
                current=current_value,
                previous=previous_value,
                delta=_delta(current_value, previous_value),
                change_24h=_window_delta(observations, field, latest_time - timedelta(hours=24)),
                change_30d=_window_delta(observations, field, latest_time - timedelta(days=30)),
                last_regression_at=(
                    _last_regression(observations, field) if field in _REGRESSION_FIELDS else None
                ),
            )
        return result

    def _volumes(self, now: datetime) -> tuple[PublicVolume, ...]:
        rows = self._connection.execute(
            """SELECT volumes.public_volume_id, volumes.display_name, volumes.label,
                volumes.filesystem, physical_disks.public_disk_id, volumes.role,
                volume_observations.observed_at, volume_observations.online,
                volume_observations.total_bytes, volume_observations.free_bytes
            FROM volumes JOIN physical_disks ON physical_disks.disk_id = volumes.disk_id
            LEFT JOIN volume_observations ON volume_observations.observation_id = (
                SELECT observation_id FROM volume_observations latest
                WHERE latest.volume_id = volumes.volume_id
                ORDER BY observation_id DESC LIMIT 1)
            ORDER BY volumes.public_volume_id"""
        ).fetchall()
        result: list[PublicVolume] = []
        for row in rows:
            total, free = row[8], row[9]
            used = total - free if total is not None and free is not None else None
            observed_at = datetime.fromisoformat(str(row[6])) if row[6] else None
            result.append(
                PublicVolume(
                    volume_id=str(row[0]),
                    display_name=str(row[1] or row[0]),
                    label=str(row[2]) if row[2] is not None else None,
                    filesystem=str(row[3]) if row[3] is not None else None,
                    disk_id=str(row[4]),
                    role=str(row[5]),
                    online=bool(row[7]) if row[7] is not None else False,
                    stale=(observed_at is None or now - observed_at > self._volume_stale_after),
                    total_bytes=total,
                    used_bytes=used,
                    free_bytes=free,
                    free_percent=(free * 100 / total if total and free is not None else None),
                    observed_at=observed_at,
                )
            )
        return tuple(result)


def _delta(current: object, previous: object) -> int | None:
    if isinstance(current, bool) or isinstance(previous, bool):
        return None
    return current - previous if isinstance(current, int) and isinstance(previous, int) else None


def _window_delta(
    observations: list[tuple[datetime, dict[str, Any]]], field: str, cutoff: datetime
) -> int | None:
    current = observations[0][1].get(field)
    if isinstance(current, bool) or not isinstance(current, int):
        return None
    candidates = [item for item in observations if item[0] <= cutoff]
    baseline = (candidates[0] if candidates else observations[-1])[1].get(field)
    return (
        current - baseline if isinstance(baseline, int) and not isinstance(baseline, bool) else None
    )


def _last_regression(
    observations: list[tuple[datetime, dict[str, Any]]], field: str
) -> datetime | None:
    for (current_time, current), (_, previous) in pairwise(observations):
        delta = _delta(current.get(field), previous.get(field))
        if delta is not None and delta > 0:
            return current_time
    return None


def _media_type(value: str | None) -> Literal["hdd", "ssd", "nvme", "unknown"]:
    normalized = (value or "").casefold()
    if "nvme" in normalized:
        return "nvme"
    if "ssd" in normalized or "solid" in normalized:
        return "ssd"
    if "hdd" in normalized or "hard" in normalized:
        return "hdd"
    return "unknown"


def _operational(value: str) -> Literal["online", "offline", "missing", "unknown"]:
    return cast(
        Literal["online", "offline", "missing", "unknown"],
        value if value in {"online", "offline", "missing"} else "unknown",
    )


def _smart_health(value: str) -> Literal["healthy", "warning", "critical", "unknown"]:
    return cast(
        Literal["healthy", "warning", "critical", "unknown"],
        value if value in _SEVERITY else "unknown",
    )


def _overall_health(
    jobs: tuple[PublicJob, ...],
    disks: tuple[PublicDisk, ...],
    issues: tuple[PublicHealthIssue, ...],
) -> Literal["healthy", "warning", "critical", "unknown"]:
    values = [job.health for job in jobs] + [disk.smart_health for disk in disks]
    values += [issue.severity for issue in issues]
    if not values:
        return "unknown"
    return max(values, key=lambda value: _SEVERITY[value])


def _backup_disk_state(
    disks: tuple[PublicDisk, ...], running: bool
) -> Literal["offline", "online_during_backup", "error", "unknown"]:
    backup_disks = [disk for disk in disks if disk.role == "backup"]
    if not backup_disks:
        return "unknown"
    states = {disk.operational_state for disk in backup_disks}
    if "missing" in states:
        return "error"
    if states == {"offline"}:
        return "offline"
    if running and states <= {"offline", "online"} and "online" in states:
        return "online_during_backup"
    return "unknown"


def _utc(value: datetime) -> datetime:
    return require_aware(value).astimezone(UTC)
