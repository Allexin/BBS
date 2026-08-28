[CmdletBinding()]
param(
    [ValidateSet('D')]
    [string]$TestDrive = 'D'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run PowerShell as Administrator.'
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$resultRoot = Join-Path $projectRoot '.poc-work\stage6'
$logPath = Join-Path $resultRoot 'mirror-hardware.log'
$resultPath = Join-Path $resultRoot 'mirror-hardware-result.json'
New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null

$partition = Get-Partition -DriveLetter $TestDrive -ErrorAction Stop
$disk = Get-Disk -Number $partition.DiskNumber -ErrorAction Stop
if ($disk.IsBoot -or $disk.IsSystem) {
    throw 'Boot and system disks are forbidden.'
}
if ($disk.IsOffline -or $disk.IsReadOnly) {
    throw 'Disposable drive D must be online and writable.'
}
if ([string]::IsNullOrWhiteSpace([string]$disk.UniqueId)) {
    throw 'Disposable disk has no stable identity.'
}

$env:BBS_RUN_STAGE6_HARDWARE = '1'
$env:BBS_HARDWARE_TEST_DRIVE = $TestDrive
$env:BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID = ([string]$disk.UniqueId).Trim()

$startedAt = [DateTimeOffset]::UtcNow
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$testOutput = @(& $python -m pytest tests/hardware/test_stage6_mirror.py -v 2>&1)
$testExitCode = $LASTEXITCODE
$testOutput | ForEach-Object { [string]$_ } | Set-Content -LiteralPath $logPath -Encoding UTF8

$result = [ordered]@{
    schema_version = 1
    status = if ($testExitCode -eq 0) { 'passed' } else { 'failed' }
    test_drive = $TestDrive
    disk_number = $disk.Number
    started_at = $startedAt.ToString('o')
    finished_at = [DateTimeOffset]::UtcNow.ToString('o')
    pytest_exit_code = $testExitCode
    log_path = $logPath
}
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultPath -Encoding UTF8

Write-Output "Result saved to: $resultPath"
Write-Output "Log saved to: $logPath"
exit $testExitCode
