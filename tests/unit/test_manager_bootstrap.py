import json
from pathlib import Path

from backup_system.common.exit_codes import ManagerExitCode
from backup_system.manager.bootstrap import bootstrap_log_path, write_bootstrap_failure
from backup_system.manager.cli import main


def test_bootstrap_log_is_fixed_under_stable_data(tmp_path: Path) -> None:
    config = tmp_path / "data" / "config" / "manager.yaml"
    assert bootstrap_log_path(config) == tmp_path / "data" / "logs" / "bootstrap.jsonl"


def test_bootstrap_failure_is_durable_jsonl(tmp_path: Path) -> None:
    config = tmp_path / "data" / "config" / "manager.yaml"
    path = write_bootstrap_failure(config, exit_code=40, diagnostic="bad config")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["event"] == "manager_bootstrap_failed"
    assert payload["exit_code"] == 40
    assert payload["diagnostic"] == "bad config"


def test_invalid_config_uses_manager_exit_code_and_persists_diagnostic(tmp_path: Path) -> None:
    config = tmp_path / "data" / "config" / "manager.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("not: a manager config\n", encoding="utf-8")

    assert main(["--config", str(config), "--validate-only"]) == ManagerExitCode.CONFIG_INVALID
    records = (tmp_path / "data" / "logs" / "bootstrap.jsonl").read_text(encoding="utf-8")
    assert "manager_bootstrap_failed" in records
