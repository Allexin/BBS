[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z]$')]
    [string]$TestDrive = 'D'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    throw 'Run PowerShell as Administrator.'
}

$TestDrive = $TestDrive.ToUpperInvariant()
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$resultRoot = Join-Path $projectRoot '.poc-work\stage0'
$preflightPath = Join-Path $resultRoot 'admin-preflight.json'
$logPath = Join-Path $resultRoot 'hardware-acceptance.log'
$resultPath = Join-Path $resultRoot 'hardware-acceptance-result.json'

New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null

& (Join-Path $PSScriptRoot 'admin_preflight.ps1') -OutputPath $preflightPath | Out-Null
$preflight = Get-Content -LiteralPath $preflightPath -Raw -Encoding UTF8 | ConvertFrom-Json
$matches = @($preflight.disks | Where-Object {
    @($_.partitions.drive_letter) -contains $TestDrive
})
if ($matches.Count -ne 1) {
    throw "Preflight must identify exactly one online disk for drive $TestDrive."
}
$disk = $matches[0]
if ($disk.is_boot -or $disk.is_system) {
    throw 'Boot and system disks are forbidden.'
}
if ($disk.is_offline -or $disk.is_read_only) {
    throw 'The disposable test disk must initially be online and writable.'
}
if ([string]::IsNullOrWhiteSpace([string]$disk.unique_id)) {
    throw 'The disposable test disk has no stable UniqueId.'
}

$env:BBS_RUN_STAGE0_HARDWARE = '1'
$env:BBS_HARDWARE_TEST_DRIVE = $TestDrive
$env:BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID = ([string]$disk.unique_id).Trim()

$startedAt = [DateTimeOffset]::UtcNow
$testOutput = @(& py -m uv run pytest tests/hardware/test_stage0_windows.py -v 2>&1)
$testExitCode = $LASTEXITCODE
$testOutput | ForEach-Object { [string]$_ } | Set-Content -LiteralPath $logPath -Encoding UTF8

$result = [ordered]@{
    schema_version = 1
    status = if ($testExitCode -eq 0) { 'passed' } else { 'failed' }
    test_drive = $TestDrive
    disk_number = $disk.number
    started_at = $startedAt.ToString('o')
    finished_at = [DateTimeOffset]::UtcNow.ToString('o')
    pytest_exit_code = $testExitCode
    log_path = $logPath
}
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultPath -Encoding UTF8

Write-Output "Result saved to: $resultPath"
Write-Output "Log saved to: $logPath"
exit $testExitCode
