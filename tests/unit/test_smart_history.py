from datetime import UTC, datetime, timedelta
from pathlib import Path

from backup_system.manager.database import open_manager_database
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.smart_history import (
    SmartComparison,
    SmartHistoryRepository,
    SmartMetrics,
    SmartSeverity,
)


def _record(
    repository: SmartHistoryRepository,
    metrics: SmartMetrics,
    *,
    identity: str = "TEST-SERIAL",
    success: bool = True,
    offset: int = 0,
) -> SmartComparison:
    return repository.record(
        disk_id="source-main",
        public_disk_id="disk-1",
        identity_key=identity,
        role="source",
        observed_at=datetime.now(UTC) + timedelta(seconds=offset),
        operational_state="online",
        smart_health="healthy" if success else "unknown",
        metrics=metrics,
        collection_success=success,
    )


def test_first_sample_creates_baseline_and_growth_creates_regression(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    try:
        repository = SmartHistoryRepository(connection)
        first = _record(repository, SmartMetrics(pending_sectors=0, interface_crc_errors=2))
        second = _record(
            repository,
            SmartMetrics(pending_sectors=1, interface_crc_errors=3),
            offset=1,
        )
        stable = _record(
            repository,
            SmartMetrics(pending_sectors=1, interface_crc_errors=3),
            offset=2,
        )
        assert first.baseline_created and first.regressions == ()
        assert {(item.rule_id, item.severity) for item in second.regressions} == {
            ("pending_sectors", SmartSeverity.CRITICAL),
            ("interface_crc_errors", SmartSeverity.WARNING),
        }
        assert stable.regressions == ()
    finally:
        connection.close()


def test_unknown_and_replacement_do_not_create_false_regression(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    try:
        repository = SmartHistoryRepository(connection)
        _record(repository, SmartMetrics(reallocated_sectors=10))
        unknown = _record(
            repository, SmartMetrics(reallocated_sectors=100), success=False, offset=1
        )
        replacement = _record(
            repository,
            SmartMetrics(reallocated_sectors=100),
            identity="REPLACEMENT",
            offset=2,
        )
        assert unknown.regressions == () and not unknown.baseline_created
        assert replacement.regressions == () and replacement.baseline_created
    finally:
        connection.close()


def test_counter_decrease_is_reported_as_reset_not_recovery(tmp_path: Path) -> None:
    connection = open_manager_database(tmp_path / "manager.sqlite3")
    try:
        repository = SmartHistoryRepository(connection)
        _record(repository, SmartMetrics(nvme_media_errors=5))
        comparison = _record(repository, SmartMetrics(nvme_media_errors=1), offset=1)
        assert comparison.regressions == ()
        assert comparison.reset_counters == ("nvme_media_errors",)
    finally:
        connection.close()


def test_regression_alert_is_durable_and_unchanged_value_does_not_duplicate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manager.sqlite3"
    connection = open_manager_database(path)
    try:
        repository = SmartHistoryRepository(connection, NotificationRepository(connection))
        _record(repository, SmartMetrics(pending_sectors=0))
        _record(repository, SmartMetrics(pending_sectors=1), offset=1)
        _record(repository, SmartMetrics(pending_sectors=1), offset=2)
        assert connection.execute("SELECT kind FROM notifications").fetchall() == [
            ("smart_regression",)
        ]
    finally:
        connection.close()

    connection = open_manager_database(path)
    try:
        repository = SmartHistoryRepository(connection, NotificationRepository(connection))
        _record(repository, SmartMetrics(pending_sectors=1), offset=3)
        assert connection.execute("SELECT count(*) FROM notifications").fetchone() == (1,)
    finally:
        connection.close()
