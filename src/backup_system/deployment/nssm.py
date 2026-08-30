"""Exact NSSM service configuration with read-back verification."""

from __future__ import annotations

import subprocess
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
        NssmSetting("AppKillProcessTree", "0"),
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
    verify_service_settings(nssm=nssm, service_name=service_name, root=root, run=runner)


def verify_service_settings(
    *, nssm: Path, service_name: str, root: Path, run: CommandRunner | None = None
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


def _checked(runner: CommandRunner, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    result = runner(argv)
    if result.returncode != 0:
        raise NssmConfigurationError(
            f"NSSM command failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, capture_output=True, text=True, shell=False)
