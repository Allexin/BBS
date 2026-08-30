"""Exact NSSM service configuration with read-back verification."""

from __future__ import annotations

import subprocess
import winreg
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from backup_system.common.exit_codes import ManagerExitCode

INFINITE_WINDOWS_WAIT_MS = 0xFFFFFFFF
STOP_METHOD_SKIP = 2 | 4 | 8


class NssmConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NssmSetting:
    name: str
    value: str
    subparameter: str | None = None


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
RegistryWriter = Callable[[str, int], None]
RegistryReader = Callable[[str], int]


def service_settings(root: Path) -> tuple[NssmSetting, ...]:
    return (
        NssmSetting("ObjectName", "LocalSystem"),
        NssmSetting("AppDirectory", str(root)),
        NssmSetting("Start", "SERVICE_AUTO_START"),
        NssmSetting("AppExit", "Restart", "Default"),
        NssmSetting("AppExit", "Exit", str(int(ManagerExitCode.CONFIG_INVALID))),
        NssmSetting("AppExit", "Exit", str(int(ManagerExitCode.BOOTSTRAP_ERROR))),
        NssmSetting("AppRestartDelay", "10000"),
        NssmSetting("AppStopMethodSkip", str(STOP_METHOD_SKIP)),
        NssmSetting("AppStopMethodConsole", str(INFINITE_WINDOWS_WAIT_MS)),
        NssmSetting("AppStdout", str(root / "data" / "logs" / "manager-stdout.log")),
        NssmSetting("AppStderr", str(root / "data" / "logs" / "manager-stderr.log")),
    )


def configure_service(
    *,
    nssm: Path,
    service_name: str,
    root: Path,
    run: CommandRunner | None = None,
    write_registry: RegistryWriter | None = None,
    read_registry: RegistryReader | None = None,
) -> None:
    runner = run or _run
    python = root / ".venv" / "Scripts" / "python.exe"
    config = root / "data" / "config" / "manager.yaml"
    status = runner([str(nssm), "status", service_name])
    if status.returncode == 0:
        _checked(runner, [str(nssm), "set", service_name, "Application", str(python)])
        _checked(
            runner,
            [
                str(nssm), "set", service_name, "AppParameters", "-m",
                "backup_system.manager", "--config", str(config),
            ],
        )
    else:
        _checked(
            runner,
            [
                str(nssm), "install", service_name, str(python), "-m",
                "backup_system.manager", "--config", str(config),
            ],
        )
    for setting in service_settings(root):
        command = [str(nssm), "set", service_name, setting.name]
        if setting.subparameter is not None:
            command.append(setting.subparameter)
        command.append(setting.value)
        _checked(runner, command)
    writer = write_registry or _registry_writer(service_name)
    writer("AppKillProcessTree", 0)
    verify_service_settings(
        nssm=nssm,
        service_name=service_name,
        root=root,
        run=runner,
        read_registry=read_registry,
    )


def verify_service_settings(
    *,
    nssm: Path,
    service_name: str,
    root: Path,
    run: CommandRunner | None = None,
    read_registry: RegistryReader | None = None,
) -> None:
    runner = run or _run
    for setting in service_settings(root):
        command = [str(nssm), "get", service_name, setting.name]
        if setting.subparameter is not None:
            command.append(setting.subparameter)
        result = _checked(runner, command)
        actual = result.stdout.strip()
        if actual.casefold() != setting.value.casefold():
            label = f"{setting.name} {setting.subparameter or ''}".strip()
            raise NssmConfigurationError(
                f"NSSM setting {label} read back as {actual!r}, expected {setting.value!r}"
            )
    reader = read_registry or _registry_reader(service_name)
    if reader("AppKillProcessTree") != 0:
        raise NssmConfigurationError("NSSM registry setting AppKillProcessTree is not 0")


def _registry_path(service_name: str) -> str:
    if not service_name or "\\" in service_name:
        raise NssmConfigurationError("invalid Windows service name")
    return rf"SYSTEM\CurrentControlSet\Services\{service_name}\Parameters"


def _registry_writer(service_name: str) -> RegistryWriter:
    def write(name: str, value: int) -> None:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _registry_path(service_name),
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
            winreg.FlushKey(key)

    return write


def _registry_reader(service_name: str) -> RegistryReader:
    def read(name: str) -> int:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _registry_path(service_name),
            0,
            winreg.KEY_QUERY_VALUE,
        ) as key:
            value, value_type = winreg.QueryValueEx(key, name)
        if value_type != winreg.REG_DWORD or not isinstance(value, int):
            raise NssmConfigurationError(f"NSSM registry setting {name} is not REG_DWORD")
        return value

    return read


def _checked(runner: CommandRunner, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    result = runner(argv)
    if result.returncode != 0:
        raise NssmConfigurationError(
            f"NSSM command failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, capture_output=True, text=True, shell=False)
