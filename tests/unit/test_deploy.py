from pathlib import Path

import pytest

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
