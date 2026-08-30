param(
    [string]$Stable = 'S:\BasovBackupSystem\BBS_Stable',
    [string]$Service = 'BBS',
    [Parameter(Mandatory = $true)]
    [string]$NginxAccount,
    [Parameter(Mandatory = $true)]
    [string]$DeploymentAccount
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$source = Join-Path $projectRoot '.poc-work\tools\smartmontools\bin\smartctl.exe'
$restartSource = Join-Path $projectRoot 'restart-bbs.bat'

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this one-time native-tool setup from an elevated PowerShell.'
    }
    $statusText = (& nssm status $Service 2>&1 | Out-String)
    $status = $statusText -replace '[^A-Z_]', ''
    if ($status -notmatch 'SERVICE_STOPPED') {
        throw "Service '$Service' must be stopped; current status: $statusText"
    }
    if (-not (Test-Path $source -PathType Leaf)) {
        throw 'Pinned Dev smartctl 7.5 is missing.'
    }
    if (-not (Test-Path $restartSource -PathType Leaf)) {
        throw 'Stable restart launcher is missing from Dev.'
    }
    $stableRoot = (Resolve-Path $Stable).Path
    if (-not (Test-Path (Join-Path $stableRoot 'backup-system.root') -PathType Leaf)) {
        throw 'Stable root marker is missing.'
    }
    Write-Output '[1/2] Applying the approved Stable ACL policy.'
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $aclOutput = Join-Path $projectRoot '.poc-work\stage10-smart-acl-result.json'
    & $python (Join-Path $projectRoot 'poc\corrective\apply_stable_acl.py') --stable $stableRoot --nginx-account $NginxAccount --deployment-account $DeploymentAccount --output $aclOutput
    if ($LASTEXITCODE -ne 0) {
        throw "Stable ACL setup failed with exit code $LASTEXITCODE."
    }
    Write-Output '[2/2] Installing pinned smartctl and the Stable restart launcher.'
    $bin = Join-Path $stableRoot 'bin'
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination (Join-Path $bin 'smartctl.exe') -Force
    Copy-Item -LiteralPath $restartSource -Destination (Join-Path $stableRoot 'restart-bbs.bat') -Force
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $bin 'smartctl.exe')).Hash
    Write-Output "Pinned smartctl installed once. SHA256: $hash"
}
catch {
    Write-Error $_
    exit 1
}
