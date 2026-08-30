import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from backup_system.deployment.nssm import (
    INFINITE_WINDOWS_WAIT_MS,
    STOP_METHOD_SKIP,
    NssmConfigurationError,
    configure_service,
    service_settings,
)


def test_nssm_settings_enforce_cooperative_stop_and_restart_policy(tmp_path: Path) -> None:
    settings = {(item.name, item.subparameter): item.value for item in service_settings(tmp_path)}
    assert settings[("ObjectName", None)] == "LocalSystem"
    assert settings[("AppExit", "Default")] == "Restart"
    assert settings[("AppExit", "40")] == "Exit"
    assert settings[("AppRestartDelay", None)] == "10000"
    assert settings[("AppStopMethodSkip", None)] == str(STOP_METHOD_SKIP)
    assert settings[("AppStopMethodConsole", None)] == str(INFINITE_WINDOWS_WAIT_MS)


class _Nssm:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str | None], str] = {}
        self.commands: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.commands.append(command)
        if command[1] == "set":
            subparameter = command[4] if len(command) == 6 else None
            self.values[(command[3], subparameter)] = command[-1]
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "get":
            subparameter = command[4] if len(command) == 5 else None
            value = self.values[(command[3], subparameter)]
            return subprocess.CompletedProcess(command, 0, value + "\n", "")
        if command[1] == "status":
            return subprocess.CompletedProcess(command, 1, "", "missing")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_configuration_uses_argv_and_verifies_every_setting(tmp_path: Path) -> None:
    fake = _Nssm()
    registry: dict[str, int] = {}
    configure_service(
        nssm=tmp_path / "nssm.exe",
        service_name="BBS",
        root=tmp_path,
        run=fake,
        write_registry=registry.__setitem__,
        read_registry=registry.__getitem__,
    )
    install = fake.commands[1]
    assert install[1:4] == ["install", "BBS", str(tmp_path / ".venv/Scripts/python.exe")]
    assert sum(command[1] == "get" for command in fake.commands) == len(service_settings(tmp_path))


def test_readback_mismatch_blocks_configuration(tmp_path: Path) -> None:
    fake = _Nssm()

    def corrupt(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        result = fake(argv)
        if list(argv)[1:4] == ["get", "BBS", "AppRestartDelay"]:
            return subprocess.CompletedProcess(list(argv), 0, "1\n", "")
        return result

    with pytest.raises(NssmConfigurationError, match="read back"):
        configure_service(
            nssm=tmp_path / "nssm.exe",
            service_name="BBS",
            root=tmp_path,
            run=corrupt,
            write_registry=lambda name, value: None,
            read_registry=lambda name: 0,
        )


def test_existing_service_is_updated_without_reinstall(tmp_path: Path) -> None:
    fake = _Nssm()

    def existing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(argv)[1] == "status":
            return subprocess.CompletedProcess(list(argv), 0, "SERVICE_STOPPED\n", "")
        return fake(argv)

    configure_service(
        nssm=tmp_path / "nssm.exe",
        service_name="BBS",
        root=tmp_path,
        run=existing,
        write_registry=lambda name, value: None,
        read_registry=lambda name: 0,
    )
    assert not any(command[1] == "install" for command in fake.commands)
    assert any(command[1:4] == ["set", "BBS", "Application"] for command in fake.commands)
    assert any(command[1:4] == ["set", "BBS", "AppParameters"] for command in fake.commands)


def test_registry_readback_mismatch_blocks_configuration(tmp_path: Path) -> None:
    fake = _Nssm()
    with pytest.raises(NssmConfigurationError, match="AppKillProcessTree"):
        configure_service(
            nssm=tmp_path / "nssm.exe",
            service_name="BBS",
            root=tmp_path,
            run=fake,
            write_registry=lambda name, value: None,
            read_registry=lambda name: 1,
        )
