$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$smartctlPath = Join-Path $projectRoot '.poc-work\tools\smartmontools\bin\smartctl.exe'
$resultPath = Join-Path $projectRoot '.poc-work\stage10-smart-web\result.json'

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this acceptance from an elevated PowerShell.'
    }
    if (-not (Test-Path $pythonPath -PathType Leaf)) {
        throw 'Project virtual environment Python is missing.'
    }
    if (-not (Test-Path $smartctlPath -PathType Leaf)) {
        throw 'Local smartctl 7.5 is missing from .poc-work tools.'
    }
    Write-Output '[1/4] Resolving drive D to one physical disk.'
    $partition = Get-Partition -DriveLetter D -ErrorAction Stop
    $disk = $partition | Get-Disk -ErrorAction Stop
    if (@($disk).Count -ne 1 -or $disk.OperationalStatus -notcontains 'Online') {
        throw 'Drive D must resolve to one online test disk.'
    }
    $serial = ([string]$disk.SerialNumber).Trim()
    if ([string]::IsNullOrWhiteSpace($serial)) {
        throw 'Test disk serial is unavailable.'
    }
    $device = '/dev/pd' + [string]$disk.Number
    Write-Output '[2/4] Starting the short SMART self-test on test disk D only.'
    & $pythonPath (Join-Path $PSScriptRoot 'smart_web_acceptance.py') --smartctl $smartctlPath --device $device --serial $serial --size ([string]$disk.Size)
    if ($LASTEXITCODE -ne 0) {
        throw "SMART/Web acceptance exited with code $LASTEXITCODE."
    }
    Write-Output '[3/4] SMART history and static Web projection were published.'
    Write-Output '[4/4] Acceptance completed.'
    Write-Output "Result saved to: $resultPath"
}
catch {
    Write-Error "SMART/Web acceptance failed. See $resultPath"
    exit 1
}
