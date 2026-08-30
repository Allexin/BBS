import subprocess
from pathlib import Path

import pytest

from backup_system.deployment import deploy as deploy_module
from backup_system.deployment.deploy import DeploymentError, build_parser, deploy


def test_deploy_cli_requires_explicit_stable_root() -> None:
    parsed = build_parser().parse_args(["--stable", r"C:\BackupSystem\Stable"])
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
            nssm=tmp_path / "nssm.exe",
            uv=tmp_path / "uv.exe",
        )


def test_deploy_stops_switches_preserves_data_and_restarts(
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

    def stop(nssm: Path, service: str) -> None:
        del nssm, service
        assert (stable / "app/old.txt").is_file()
        calls.append("stop")

    def command(argv, *, env=None) -> subprocess.CompletedProcess[str]:
        del env
        if list(argv)[1:3] == ["start", "BBS-Test"]:
            calls.append("start")
        else:
            calls.append("prepare")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def configure(**kwargs: object) -> None:
        del kwargs
        assert (stable / "app/new.txt").is_file()
        calls.append("configure")

    monkeypatch.setattr(deploy_module, "stage_release", stage)
    monkeypatch.setattr(deploy_module, "_stop_service_if_installed", stop)
    monkeypatch.setattr(deploy_module, "_run_checked", command)
    monkeypatch.setattr(deploy_module, "configure_service", configure)
    monkeypatch.setattr(deploy_module, "_wait_for_status", lambda *args, **kwargs: None)

    revision = deploy(
        source=source,
        stable=stable,
        service_name="BBS-Test",
        nssm=tmp_path / "nssm.exe",
        uv=tmp_path / "uv.exe",
    )

    assert revision == "a" * 40
    assert calls == ["stop", "prepare", "prepare", "configure", "start"]
    assert config.read_text(encoding="ascii") == "stable-data"
    assert not (stable / "app/old.txt").exists()
