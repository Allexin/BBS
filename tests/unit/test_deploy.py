import subprocess
from pathlib import Path

import pytest

from backup_system.deployment import deploy as deploy_module
from backup_system.deployment.deploy import (
    DeploymentError,
    build_parser,
    deploy,
    initialize_stable,
)


def test_deploy_cli_requires_explicit_stable_root() -> None:
    parsed = build_parser().parse_args(
        ["--stable", r"C:\BackupSystem\Stable", "--nginx-account", "BBS-Web"]
    )
    assert parsed.stable == Path(r"C:\BackupSystem\Stable")


def test_deploy_rejects_nested_dev_and_stable_before_commands(tmp_path: Path) -> None:
    source = tmp_path / "dev"
    stable = source / "stable"
    stable.mkdir(parents=True)
    with pytest.raises(DeploymentError, match="separate"):
        deploy(
            source=source,
            stable=stable,
            service_name="BBS-Test",
            nginx_account="BBS-Web",
            nssm=tmp_path / "nssm.exe",
            uv=tmp_path / "uv.exe",
        )


def test_deploy_requires_existing_stable_marker(tmp_path: Path) -> None:
    source = tmp_path / "dev"
    stable = tmp_path / "stable"
    source.mkdir()
    stable.mkdir()
    with pytest.raises(DeploymentError, match="marker"):
        deploy(
            source=source,
            stable=stable,
            service_name="BBS-Test",
            nginx_account="BBS-Web",
            nssm=tmp_path / "nssm.exe",
            uv=tmp_path / "uv.exe",
        )


def test_initialize_stable_preserves_credentials_and_creates_disabled_config(
    tmp_path: Path,
) -> None:
    stable = tmp_path / "stable"
    config = stable / "data" / "config"
    config.mkdir(parents=True)
    credentials = config / "telegram.json"
    credentials.write_text("secret-test-value", encoding="ascii")

    initialize_stable(stable)

    assert (stable / "backup-system.root").is_file()
    assert "jobs: []" in (config / "manager.yaml").read_text(encoding="utf-8")
    assert "disks: []" in (config / "smart.yaml").read_text(encoding="utf-8")
    assert credentials.read_text(encoding="ascii") == "secret-test-value"
    with pytest.raises(DeploymentError, match="requires missing"):
        initialize_stable(stable)


def test_deploy_requires_stopped_switches_preserves_data_and_waits_for_manual_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "dev"
    stable = tmp_path / "stable"
    source.mkdir()
    stable.mkdir()
    (stable / "backup-system.root").write_text("marker", encoding="ascii")
    config = stable / "data/config/manager.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("stable-data", encoding="ascii")
    (stable / "app").mkdir()
    (stable / "app/old.txt").write_text("old", encoding="ascii")
    (source / "deployment-manifest.json").write_text("{}", encoding="ascii")
    calls: list[str] = []

    monkeypatch.setattr(deploy_module, "_git_revision", lambda root: "a" * 40)
    monkeypatch.setattr(deploy_module, "load_deployment_manifest", lambda path: object())

    def stage(root: Path, staging: Path, manifest: object) -> None:
        del root, manifest
        (staging / "app").mkdir(parents=True)
        (staging / "app/new.txt").write_text("new", encoding="ascii")
        (staging / ".venv/Scripts").mkdir(parents=True)
        (staging / ".venv/Scripts/python.exe").touch()
        (staging / "web").mkdir()

    def require_stopped(nssm: Path, service: str) -> None:
        del nssm, service
        assert (stable / "app/old.txt").is_file()
        calls.append("require-stopped")

    def command(argv, *, env=None) -> subprocess.CompletedProcess[str]:
        del env
        calls.append("prepare")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def configure(**kwargs: object) -> None:
        del kwargs
        assert (stable / "app/new.txt").is_file()
        calls.append("configure")

    monkeypatch.setattr(deploy_module, "stage_release", stage)
    monkeypatch.setattr(deploy_module, "_require_service_stopped", require_stopped)
    monkeypatch.setattr(deploy_module, "_run_checked", command)
    monkeypatch.setattr(deploy_module, "configure_service", configure)
    monkeypatch.setattr(
        deploy_module,
        "apply_stable_acls",
        lambda stable, nginx_account: calls.append("acl"),
    )
    monkeypatch.setattr(deploy_module, "_wait_for_status", lambda *args, **kwargs: None)

    revision = deploy(
        source=source,
        stable=stable,
        service_name="BBS-Test",
        nginx_account="BBS-Web",
        nssm=tmp_path / "nssm.exe",
        uv=tmp_path / "uv.exe",
    )

    assert revision == "a" * 40
    assert calls == ["require-stopped", "prepare", "prepare", "acl", "configure"]
    assert config.read_text(encoding="ascii") == "stable-data"
    assert not (stable / "app/old.txt").exists()


def test_deploy_refuses_to_stop_a_running_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        deploy_module,
        "_run",
        lambda argv, env=None: subprocess.CompletedProcess(
            list(argv), 0, "SERVICE_RUNNING\x00", ""
        ),
    )

    with pytest.raises(DeploymentError, match="must be stopped manually"):
        deploy_module._require_service_stopped(tmp_path / "nssm.exe", "BBS-Test")
