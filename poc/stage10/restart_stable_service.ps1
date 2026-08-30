param(
    [string]$Stable = 'S:\BasovBackupSystem\BBS_Stable',
    [string]$Service = 'BBS',
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run the validated restart from an elevated PowerShell.'
    }
    $nssm = (Get-Command nssm.exe -CommandType Application -ErrorAction Stop).Source
    & $python -m backup_system.deployment.restart --stable $Stable --service $Service --nssm $nssm --timeout-seconds ([string]$TimeoutSeconds)
    if ($LASTEXITCODE -ne 0) {
        throw "Validated BBS restart failed with exit code $LASTEXITCODE."
    }
}
catch {
    Write-Error $_
    exit 1
}
