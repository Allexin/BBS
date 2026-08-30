from pathlib import Path
from uuid import uuid4

import pytest

from backup_system.common.config import MaintenanceJobConfig
from backup_system.common.config_io import (
    ConfigLoadError,
    load_manager_config,
    validate_config_tree,
    validate_job_with_owner,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repository(marker_uuid: str) -> str:
    return f"""repository:
  engine: restic
  repository_id: primary
  path: 'C:\\Backup\\restic'
  marker_uuid: '{marker_uuid}'
  encryption: {{mode: none}}
  marker_file: 'C:\\Backup\\.backup-volume.json'
disk:
  physical_serial: TEST-SERIAL
  expected_size_bytes: 1000000
  partition_guid: partition-guid
  volume_guid: volume-guid
  mount_point: 'C:\\BackupVolumes\\primary'
  repository_path_timeout_seconds: 30
"""


def _write_valid_tree(config_dir: Path) -> Path:
    marker_uuid = str(uuid4())
    manager = config_dir / "manager.yaml"
    _write(
        manager,
        """schema_version: 1
timezone: Europe/Samara
scheduler: {poll_seconds: 5}
monitoring:
  volumes: {poll_seconds: 60, items: []}
jobs:
  - id: data
    enabled: true
    display_name: Data
    schedule:
      cron: '0 0 * * 1'
      timezone: Europe/Samara
      deadline: '08:00'
      cycle: [{operation: backup}]
  - id: data-maintenance
    enabled: true
    display_name: Maintenance
    schedule:
      cron: '0 0 1 * *'
      timezone: Europe/Samara
      deadline: '08:00'
      cycle: [{operation: prune}]
telegram:
  enabled: false
  credentials_file: telegram.json
  daily_report_cron: '0 9 * * *'
  daily_report_timezone: Europe/Samara
  stale_manager_minutes: 10
""",
    )
    _write(
        config_dir / "smart.yaml",
        """schema_version: 1
per_disk_timeout_seconds: 30
stale_after_hours: 48
disks: []
""",
    )
    _write(
        config_dir / "jobs" / "data.yaml",
        f"""schema_version: 1
id: data
kind: snapshot
display_name: Data
source: {{path: 'F:\\'}}
excludes: []
{_repository(marker_uuid)}backup:
  host: test-host
  tags: ['job:data']
  read_error_result: failed
retention: {{keep_last: 1, keep_daily: 0, keep_weekly: 4, keep_monthly: 6, keep_yearly: 0}}
verification: {{data_subset_parts: 4, restore_test_paths: []}}
""",
    )
    _write(
        config_dir / "jobs" / "data-maintenance.yaml",
        f"""schema_version: 1
id: data-maintenance
kind: maintenance
display_name: Maintenance
repository_owner_job_id: data
{_repository(marker_uuid)}""",
    )
    return manager


def test_load_manager_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    path = tmp_path / "manager.yaml"
    _write(path, "- not\n- a\n- mapping\n")
    with pytest.raises(ConfigLoadError, match="must contain a YAML mapping"):
        load_manager_config(path)


def test_config_tree_validates_maintenance_owner(tmp_path: Path) -> None:
    manager_path = _write_valid_tree(tmp_path)
    manager, smart = validate_config_tree(manager_path)
    assert len(manager.jobs) == 2
    assert smart.disks == ()
    assert isinstance(validate_job_with_owner(tmp_path, "data-maintenance"), MaintenanceJobConfig)


def test_maintenance_mismatch_is_rejected(tmp_path: Path) -> None:
    _write_valid_tree(tmp_path)
    path = tmp_path / "jobs" / "data-maintenance.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("1000000", "2000000"), encoding="utf-8"
    )
    with pytest.raises(ConfigLoadError, match="must exactly match"):
        validate_job_with_owner(tmp_path, "data-maintenance")


def test_manager_cycle_must_match_executor_kind(tmp_path: Path) -> None:
    manager_path = _write_valid_tree(tmp_path)
    manager_path.write_text(
        manager_path.read_text(encoding="utf-8").replace(
            "cycle: [{operation: prune}]", "cycle: [{operation: backup}]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigLoadError, match="incompatible with maintenance"):
        validate_config_tree(manager_path)


def test_smart_test_job_must_reference_allowlisted_disk(tmp_path: Path) -> None:
    manager_path = _write_valid_tree(tmp_path)
    manager_path.write_text(
        manager_path.read_text(encoding="utf-8").replace(
            "jobs:\n",
            "jobs:\n"
            "  - id: test-disk-health\n"
            "    enabled: true\n"
            "    display_name: Test disk health\n"
            "    schedule:\n"
            "      cron: '0 3 * * 0'\n"
            "      timezone: Europe/Samara\n"
            "      cycle: [{operation: smart-test}]\n",
        ),
        encoding="utf-8",
    )
    _write(
        tmp_path / "jobs" / "test-disk-health.yaml",
        """schema_version: 1
id: test-disk-health
kind: smart-test
display_name: Test disk health
target:
  mode: configured-disk
  disk_id: test-disk
test_type: short
poll_seconds: 30
timeout_seconds: 900
""",
    )
    with pytest.raises(ConfigLoadError, match="outside SMART allowlist"):
        validate_config_tree(manager_path)
