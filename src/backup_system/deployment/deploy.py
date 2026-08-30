"""Observed, non-rollback Dev-to-Stable deployment workflow."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4

from backup_system.deployment.manifest import load_deployment_manifest, stage_release
from backup_system.deployment.nssm import configure_service

SERVICE_STOPPED = "SERVICE_STOPPED"
SERVICE_RUNNING = "SERVICE_RUNNING"
SERVICE_STOP_PENDING = "SERVICE_STOP_PENDING"
SEE_MASK_NOCLOSEPROCESS = 0x00000040
INFINITE = 0xFFFFFFFF


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


class DeploymentError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bbs-deploy")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--stable", type=Path, required=True)
    parser.add_argument("--service", default="BBS")
    parser.add_argument("--nssm", type=Path)
    parser.add_argument("--uv", type=Path)
    return parser


def deploy(
    *, source: Path, stable: Path, service_name: str, nssm: Path, uv: Path
) -> str:
    source = source.resolve(strict=True)
    stable = stable.resolve(strict=True)
    if source == stable or source.is_relative_to(stable) or stable.is_relative_to(source):
        raise DeploymentError("Dev and Stable trees must be separate")
    if not (stable / "backup-system.root").is_file():
        raise DeploymentError("Stable root marker is missing")
    if not (stable / "data" / "config" / "manager.yaml").is_file():
        raise DeploymentError("Stable manager config is missing")
    revision = _git_revision(source)
    manifest = load_deployment_manifest(source / "deployment-manifest.json")
    staging = stable.parent / f".{stable.name}-staging-{uuid4()}"
    switched = False
    try:
        _stop_service_if_installed(nssm, service_name)
        stage_release(source, staging, manifest)
        (staging / "backup-system.root").write_text("BBS Stable root\n", encoding="ascii")
        _run_checked(
            [
                str(uv), "sync", "--frozen", "--no-editable", "--project",
                str(staging / "app"),
            ],
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(staging / ".venv")},
        )
        staged_python = staging / ".venv" / "Scripts" / "python.exe"
        _run_checked(
            [
                str(staged_python), "-m", "backup_system.manager", "--config",
                str(stable / "data" / "config" / "manager.yaml"), "--validate-only",
            ]
        )
        for name in ("app", ".venv", "web"):
            target = stable / name
            if target.exists():
                shutil.rmtree(target)
            prepared = staging / name
            if prepared.exists():
                shutil.move(str(prepared), target)
        switched = True
        configure_service(nssm=nssm, service_name=service_name, root=stable)
        _run_checked([str(nssm), "start", service_name])
        _wait_for_status(nssm, service_name, SERVICE_RUNNING, attempts=30)
    except BaseException as error:
        phase = "after Stable switch" if switched else "before Stable switch"
        raise DeploymentError(f"deployment failed {phase}: {error}") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return revision


def _git_revision(source: Path) -> str:
    result = _run_checked(["git", "-C", str(source), "rev-parse", "HEAD"])
    revision = result.stdout.strip()
    if len(revision) != 40:
        raise DeploymentError("Git revision is not a full commit hash")
    return revision


def _stop_service_if_installed(nssm: Path, service_name: str) -> None:
    status = _run([str(nssm), "status", service_name])
    if status.returncode != 0:
        return
    if _command_output(status) == SERVICE_STOPPED:
        return
    stopped = _run([str(nssm), "stop", service_name])
    if stopped.returncode != 0 and SERVICE_STOP_PENDING not in _command_output(stopped):
        raise DeploymentError(
            f"service stop failed ({stopped.returncode}): {_command_output(stopped)}"
        )
    _wait_for_status(nssm, service_name, SERVICE_STOPPED, attempts=None)


def _wait_for_status(
    nssm: Path, service_name: str, expected: str, *, attempts: int | None
) -> None:
    count = 0
    while True:
        status = _command_output(_run_checked([str(nssm), "status", service_name]))
        if status == expected:
            return
        count += 1
        if attempts is not None and count >= attempts:
            raise DeploymentError(f"service did not reach {expected}; current status is {status}")
        time.sleep(1)


def _run_checked(
    argv: Sequence[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = _run(argv, env=env)
    if result.returncode != 0:
        raise DeploymentError(
            f"command failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def _run(
    argv: Sequence[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, check=False, capture_output=True, text=True, shell=False, env=env
    )


def _command_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout + "\n" + result.stderr).replace("\x00", "").strip()


def _is_administrator() -> bool:
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def _run_elevated(arguments: Sequence[str]) -> int:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(_ShellExecuteInfo),)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    parameters = subprocess.list2cmdline(
        ["-m", "backup_system.deployment.deploy", *arguments]
    )
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = parameters
    info.nShow = 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    arguments = build_parser().parse_args(raw_arguments)
    if not _is_administrator():
        if os.name != "nt":
            print("deployment requires Windows", file=sys.stderr)
            return 1
        try:
            return _run_elevated(raw_arguments)
        except OSError as error:
            print(f"UAC elevation failed: {error}", file=sys.stderr)
            return 1
    nssm = arguments.nssm or arguments.stable / "bin" / "nssm.exe"
    uv = arguments.uv or Path(shutil.which("uv") or "uv.exe")
    try:
        revision = deploy(
            source=arguments.source,
            stable=arguments.stable,
            service_name=arguments.service,
            nssm=nssm,
            uv=uv,
        )
    except (DeploymentError, OSError, ValueError) as error:
        print(json.dumps({"result": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"result": "success", "revision": revision}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
