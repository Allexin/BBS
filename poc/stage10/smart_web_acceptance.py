"""Hardware acceptance for the allowlisted SMART self-test and static Web projection."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from backup_system.common.config import SmartConfig
from backup_system.manager.database import open_manager_database
from backup_system.manager.executor_events import ExecutorEventIngestor
from backup_system.manager.layout import RuntimeLayout, initialize_data_layout
from backup_system.manager.notifications import NotificationRepository
from backup_system.manager.operations import OperationsRepository, RunResult
from backup_system.manager.projection_builder import ProjectionBuilder
from backup_system.manager.public_projection import ProjectionPublisher
from backup_system.manager.smart_history import SmartHistoryRepository
from backup_system.executor.smart_events import build_smart_events
from backup_system.executor.smart_preflight import SmartPreflight, SubprocessSmartctlBackend
from backup_system.executor.smart_test import (
    SubprocessSmartSelfTestBackend,
    run_smart_self_test,
)

ROOT = Path(__file__).parents[2]
WORK = ROOT / ".poc-work" / "stage10-smart-web"
RESULT = WORK / "result.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smartctl", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--size", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        if not arguments.device.startswith("/dev/pd"):
            raise ValueError("hardware acceptance requires an explicit /dev/pdN selector")
        smart = SmartConfig.model_validate(
            {
                "schema_version": 1,
                "per_disk_timeout_seconds": 30,
                "stale_after_hours": 48,
                "disks": [
                    {
                        "id": "test-disk",
                        "display_name": "Test disk D",
                        "identity": {
                            "device": arguments.device,
                            "serial": arguments.serial,
                            "expected_size_bytes": arguments.size,
                        },
                    }
                ],
            }
        )
        layout = RuntimeLayout(WORK / "runtime")
        initialize_data_layout(layout)
        connection = open_manager_database(layout.database)
        try:
            notifications = NotificationRepository(connection)
            operations = OperationsRepository(connection, notifications)
            operations.upsert_job(
                job_id="test-disk-health",
                display_name="Test disk SMART short test",
                enabled=True,
                config_valid=True,
            )
            queued = operations.enqueue(
                deduplication_key="stage10:smart-short",
                job_id="test-disk-health",
                kind="smart-test",
                trigger_source="manual",
            )
            claimed = operations.claim_next()
            if claimed is None or claimed.operation_id != queued.operation_id:
                raise RuntimeError("SMART test operation was not claimed")

            collector = SmartPreflight(SubprocessSmartctlBackend(arguments.smartctl))
            ingestor = ExecutorEventIngestor(SmartHistoryRepository(connection, notifications))
            baseline = collector.collect(smart)
            _ingest(ingestor, baseline)
            status = run_smart_self_test(
                backend=SubprocessSmartSelfTestBackend(arguments.smartctl),
                disk=smart.disks[0],
                test_type="short",
                poll_seconds=10,
                timeout_seconds=15 * 60,
                checkpoint=lambda: None,
            )
            latest = collector.collect(smart)
            _ingest(ingestor, latest)
            operations.update_stage(claimed.run_id, "smart-test-completed")
            operations.finish_run(
                claimed.run_id,
                result=RunResult.SUCCESS,
                exit_code=0,
                disk_offline_confirmed=True,
            )

            site = WORK / "site"
            site.mkdir(parents=True, exist_ok=True)
            for source in (ROOT / "web").iterdir():
                if source.is_file():
                    shutil.copy2(source, site / source.name)
            publisher = ProjectionPublisher(site / "backup-status")
            projection = ProjectionBuilder(
                connection,
                job_kinds={"test-disk-health": "smart-test"},
            )
            now = datetime.now(UTC)
            public_status, health = projection.build(
                now=now,
                manager_started_at=now,
                manager_state="idle",
                version="stage10-acceptance",
            )
            publisher.publish(public_status, health)
            _save(
                {
                    "result": "success",
                    "test_type": "short",
                    "self_test": status.description,
                    "smart_health": public_status.disks[0].smart_health,
                    "observations": 2,
                    "site": str(site),
                    "smartctl_sha256": _sha256(arguments.smartctl),
                }
            )
        finally:
            connection.close()
    except BaseException as error:
        _save({"result": "failed", "error": type(error).__name__})
        print(f"SMART/Web acceptance failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"Result saved to: {RESULT}")
    print(f"Static test site: {WORK / 'site'}")
    return 0


def _ingest(ingestor: ExecutorEventIngestor, observations: tuple[object, ...]) -> None:
    events = build_smart_events(observations, timestamp=datetime.now(UTC))  # type: ignore[arg-type]
    for event in events:
        ingestor.ingest(event)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save(value: dict[str, object]) -> None:
    RESULT.write_text(json.dumps(value, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
