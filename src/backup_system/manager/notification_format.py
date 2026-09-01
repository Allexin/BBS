"""Plain-text Telegram rendering from sanitized notification payloads."""

from __future__ import annotations

from typing import Any

from backup_system.manager.notifications import PendingNotification


def render_notification(notification: PendingNotification) -> str:
    payload = notification.payload
    if notification.kind == "daily_report":
        return _daily(payload)
    if notification.kind == "startup_report":
        return _startup(payload)
    if notification.kind == "deadline_overrun":
        return "\n".join(
            (
                f"Deadline exceeded: {_text(payload, 'job')}",
                f"Stage: {_text(payload, 'stage', fallback='unknown')}",
                f"Elapsed: {_duration(payload, 'elapsed_seconds')}",
                f"Overrun: {_duration(payload, 'overrun_seconds')}",
            )
        )
    if notification.kind == "deadline_overrun_finished":
        return "\n".join(
            (
                f"Run finished after deadline: {_text(payload, 'job')}",
                f"Final overrun: {_duration(payload, 'overrun_seconds')}",
            )
        )
    if notification.kind == "smart_regression":
        return "\n".join(
            (
                f"SMART regression: {_text(payload, 'disk')}",
                f"Indicator: {_text(payload, 'indicator')}",
                f"Change: {_text(payload, 'previous')} -> {_text(payload, 'current')}",
                f"Severity: {_text(payload, 'severity')}",
            )
        )
    if notification.kind == "smart_critical_condition":
        return "\n".join(
            (
                f"SMART critical condition: {_text(payload, 'disk')}",
                f"Indicator: {_text(payload, 'indicator')}",
                f"Current value: {_text(payload, 'current')}",
                "This is an absolute condition, not only a trend change",
            )
        )
    if notification.kind == "schedule_overlap":
        return "\n".join(
            (
                "Scheduled jobs overlap",
                f"Running: {_text(payload, 'running_job')}",
                f"Stage: {_text(payload, 'running_stage', fallback='unknown')}",
                f"Elapsed: {_duration(payload, 'running_elapsed_seconds')}",
                f"Queued: {_text(payload, 'queued_job')}",
            )
        )
    if notification.kind == "run_failed":
        return "\n".join(
            (
                f"Operation failed: {_text(payload, 'job')}",
                f"Result: {_text(payload, 'result')}",
                f"Exit code: {_text(payload, 'exit_code')}",
            )
        )
    if notification.kind == "source_read_warning":
        return "\n".join(
            (
                f"Source read warning: {_text(payload, 'job')}",
                f"Unreadable files: {_text(payload, 'error_count')}",
                "Open the local BBS Web UI for file details",
            )
        )
    if notification.kind == "disk_offline_unconfirmed":
        return "\n".join(
            (
                f"Backup disk state is unsafe: {_text(payload, 'job')}",
                "Return to offline was not confirmed",
            )
        )
    raise ValueError(f"unsupported notification kind: {notification.kind}")


def _daily(payload: dict[str, Any]) -> str:
    backups = _string_list(payload.get("backups"))
    errors = _string_list(payload.get("errors"))
    lines = ["BBS daily report", f"Health: {_text(payload, 'health')}", ""]
    lines.append("Backup jobs:")
    lines.extend(f"- {item}" for item in backups or ("Backup jobs were not run",))
    lines.append("")
    lines.append("Errors:")
    lines.extend(f"- {item}" for item in errors or ("No errors",))
    return "\n".join(lines)


def _startup(payload: dict[str, Any]) -> str:
    interrupted = _string_list(payload.get("interrupted"))
    disk_issues = _string_list(payload.get("disk_issues"))
    backups = _string_list(payload.get("missed_backups"))
    checks = _string_list(payload.get("missed_checks"))
    other_count = payload.get("missed_other_count", 0)
    lines = [
        "BBS startup report",
        f"Manager downtime: {_duration(payload, 'downtime_seconds')}",
    ]
    lines.extend(f"Interrupted: {item}" for item in interrupted)
    lines.extend(f"Disk state: {item}" for item in disk_issues)
    lines.extend(f"Missed backup: {item}" for item in backups)
    lines.extend(f"Missed check: {item}" for item in checks)
    if isinstance(other_count, int) and other_count > 0:
        lines.append(f"Missed maintenance operations: {other_count}")
    if not interrupted and not disk_issues and not backups and not checks and not other_count:
        lines.append("No important operations were missed")
    return "\n".join(lines)


def _text(payload: dict[str, Any], key: str, *, fallback: str = "unknown") -> str:
    value = payload.get(key)
    return str(value) if value is not None and str(value) else fallback


def _duration(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, int) or value < 0:
        return "unknown"
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)
