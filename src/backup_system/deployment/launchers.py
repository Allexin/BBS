"""Portable Stable launchers that do not retain staging paths."""

from pathlib import Path


def install_portable_launchers(stable: Path) -> None:
    (stable / "backupctl.bat").write_text(
        "@echo off\r\n"
        '"%~dp0.venv\\Scripts\\python.exe" -m backup_system.ctl %*\r\n'
        "exit /b %errorlevel%\r\n",
        encoding="ascii",
        newline="",
    )
