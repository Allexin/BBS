@echo off
setlocal

set "BBS_ROOT=%~dp0."
set "BBS_PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%BBS_PYTHON%" (
  echo BBS restart failed: Stable Python is missing. 1>&2
  exit /b 1
)

where nssm.exe >nul 2>&1
if errorlevel 1 (
  echo BBS restart failed: nssm.exe is not available in PATH. 1>&2
  exit /b 1
)

"%BBS_PYTHON%" -m backup_system.deployment.restart --stable "%BBS_ROOT%" --service BBS --nssm nssm.exe
exit /b %errorlevel%
