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

function Invoke-Restic {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $output = & $script:ResticPath --repo $script:Repository --insecure-no-password --no-cache @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "restic failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')`n$($output -join "`n")"
    }
    return @($output)
}

if (-not (Test-IsAdministrator)) {
    throw 'Run PowerShell as Administrator.'
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$preflightPath = Join-Path $projectRoot '.poc-work\stage0\admin-preflight.json'
$resultPath = Join-Path $projectRoot '.poc-work\stage0\admin-hardware-result.json'
$workRoot = Join-Path $projectRoot '.poc-work\stage0\admin-hardware'
$lockPath = Join-Path $PSScriptRoot 'restic.lock.json'

if (-not (Test-Path -LiteralPath $preflightPath -PathType Leaf)) {
    throw 'Run admin_preflight.ps1 first.'
}

$preflight = Get-Content -LiteralPath $preflightPath -Raw -Encoding UTF8 | ConvertFrom-Json
$expected = @($preflight.disks | Where-Object { @($_.partitions.drive_letter) -contains $TestDrive })
if ($expected.Count -ne 1) {
    throw "Preflight must contain exactly one disk for drive $TestDrive."
}
$expectedDisk = $expected[0]
$currentDisk = Get-Disk -Number $expectedDisk.number
$currentPartition = Get-Partition -DriveLetter $TestDrive

if ($currentPartition.DiskNumber -ne $currentDisk.Number) {
    throw 'Drive letter no longer belongs to the preflight disk.'
}
if (([string]$currentDisk.UniqueId).Trim() -ne ([string]$expectedDisk.unique_id).Trim()) {
    throw 'Current disk UniqueId does not match preflight.'
}
if ($currentDisk.IsBoot -or $currentDisk.IsSystem) {
    throw 'Boot and system disks are always forbidden.'
}
if ($currentDisk.IsOffline -or $currentDisk.IsReadOnly) {
    throw 'Test disk must initially be online and writable.'
}
if ($env:BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID -ne ([string]$currentDisk.UniqueId).Trim()) {
    throw 'BACKUP_SYSTEM_HARDWARE_TEST_DISK_ID does not match the selected disk.'
}

$volume = Get-Volume -DriveLetter $TestDrive
if ($volume.FileSystem -ne 'NTFS') {
    throw 'The test volume must use NTFS.'
}

$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
$script:ResticPath = Join-Path $projectRoot ".tools\restic-$($lock.version)\restic_$($lock.version)_windows_amd64.exe"
if (-not (Test-Path -LiteralPath $script:ResticPath -PathType Leaf)) {
    throw 'Pinned restic.exe is missing.'
}
$actualHash = (Get-FileHash -LiteralPath $script:ResticPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $lock.executable_sha256) {
    throw 'restic.exe SHA-256 mismatch.'
}

$testRoot = "$($TestDrive.ToUpperInvariant()):\bbs-stage0-poc"
$script:Repository = Join-Path $testRoot 'repository'
$restoreRoot = Join-Path $testRoot 'restore'
$sourceRoot = Join-Path $workRoot 'vss-source'
$expectedWorkPrefix = [IO.Path]::GetFullPath((Join-Path $projectRoot '.poc-work\stage0'))
$actualWork = [IO.Path]::GetFullPath($workRoot)
$expectedDrivePrefix = "$($TestDrive.ToUpperInvariant()):\bbs-stage0-poc"
$actualTestRoot = [IO.Path]::GetFullPath($testRoot).TrimEnd('\')
if (-not $actualWork.StartsWith($expectedWorkPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Unsafe local work path.'
}
if ($actualTestRoot -ne $expectedDrivePrefix) {
    throw 'Unsafe test disk path.'
}

if (Test-Path -LiteralPath $workRoot) {
    Remove-Item -LiteralPath $workRoot -Recurse -Force
}
if (Test-Path -LiteralPath $testRoot) {
    Remove-Item -LiteralPath $testRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $sourceRoot -Force | Out-Null
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null

$payload = Join-Path $sourceRoot 'payload.bin'
$locked = Join-Path $sourceRoot 'locked-open.bin'
[IO.File]::WriteAllBytes($payload, [Text.Encoding]::UTF8.GetBytes('BBS VSS payload'))
[IO.File]::WriteAllBytes($locked, [Text.Encoding]::UTF8.GetBytes('BBS locked VSS payload'))
$payloadHash = (Get-FileHash -LiteralPath $payload -Algorithm SHA256).Hash
$lockedHash = (Get-FileHash -LiteralPath $locked -Algorithm SHA256).Hash

$shadowBefore = @(Get-CimInstance Win32_ShadowCopy | ForEach-Object { $_.ID })
$lockedStream = [IO.File]::Open($locked, 'Open', 'ReadWrite', 'None')
try {
    Invoke-Restic init --repository-version stable | Out-Null
    $backupOutput = Invoke-Restic backup --json --use-fs-snapshot $sourceRoot
} finally {
    $lockedStream.Dispose()
}

Invoke-Restic check --read-data | Out-Null
$namespaceOutput = Invoke-Restic ls latest --json
if (($namespaceOutput -join "`n") -match 'HarddiskVolumeShadowCopy') {
    throw 'VSS device prefix leaked into restic namespace.'
}
Invoke-Restic restore --verify latest --target $restoreRoot | Out-Null

$restoredPayload = @(Get-ChildItem -LiteralPath $restoreRoot -Filter 'payload.bin' -File -Recurse)
$restoredLocked = @(Get-ChildItem -LiteralPath $restoreRoot -Filter 'locked-open.bin' -File -Recurse)
if ($restoredPayload.Count -ne 1 -or $restoredLocked.Count -ne 1) {
    throw 'Expected restored files were not found exactly once.'
}
if ((Get-FileHash -LiteralPath $restoredPayload[0].FullName -Algorithm SHA256).Hash -ne $payloadHash) {
    throw 'Restored payload hash mismatch.'
}
if ((Get-FileHash -LiteralPath $restoredLocked[0].FullName -Algorithm SHA256).Hash -ne $lockedHash) {
    throw 'Restored locked-file hash mismatch.'
}

$shadowAfter = @(Get-CimInstance Win32_ShadowCopy | ForEach-Object { $_.ID })
$orphanIds = @($shadowAfter | Where-Object { $_ -notin $shadowBefore })
if ($orphanIds.Count -ne 0) {
    throw 'A new VSS snapshot remained after restic completed.'
}

$offlineObserved = $false
$onlineRestored = $false
try {
    Set-Disk -Number $currentDisk.Number -IsOffline $true
    Start-Sleep -Seconds 2
    $offlineObserved = (Get-Disk -Number $currentDisk.Number).IsOffline
    if (-not $offlineObserved) {
        throw 'Disk did not enter offline state.'
    }
} finally {
    Set-Disk -Number $currentDisk.Number -IsOffline $false -ErrorAction Continue
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        if (-not (Get-Disk -Number $currentDisk.Number).IsOffline -and (Test-Path -LiteralPath "$TestDrive`:\")) {
            $onlineRestored = $true
            break
        }
        Start-Sleep -Seconds 2
    }
}
if (-not $onlineRestored) {
    throw "Disk did not return online. Run: Set-Disk -Number $($currentDisk.Number) -IsOffline `$false"
}

$summaryPresent = @(($backupOutput | ForEach-Object {
    try { $_ | ConvertFrom-Json -ErrorAction Stop } catch { $null }
}) | Where-Object { $_.message_type -eq 'summary' }).Count -gt 0

$result = [ordered]@{
    schema_version = 1
    status = 'passed'
    test_drive = $TestDrive.ToUpperInvariant()
    disk_number = $currentDisk.Number
    restic_version = (& $script:ResticPath version) -join "`n"
    vss_locked_file_backup = 'passed'
    namespace_device_prefix_absent = $true
    repository_check = 'passed'
    restore_hash_verification = 'passed'
    no_new_vss_orphan = $true
    backup_summary_present = $summaryPresent
    disk_offline_observed = $offlineObserved
    disk_online_restored = $onlineRestored
}
New-Item -ItemType Directory -Path (Split-Path -Parent $resultPath) -Force | Out-Null
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $resultPath -Encoding UTF8
Write-Output "Result saved to: $resultPath"
