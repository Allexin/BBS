[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    throw 'Запустите PowerShell от имени администратора.'
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$lockPath = Join-Path $PSScriptRoot 'restic.lock.json'
$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
$resticPath = Join-Path $projectRoot ".tools\restic-$($lock.version)\restic_$($lock.version)_windows_amd64.exe"

$resticStatus = 'missing'
$resticVersion = $null
if (Test-Path -LiteralPath $resticPath -PathType Leaf) {
    $actualHash = (Get-FileHash -LiteralPath $resticPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $lock.executable_sha256) {
        throw "SHA-256 restic.exe не совпадает с restic.lock.json: $actualHash"
    }
    $resticStatus = 'verified'
    $resticVersion = (& $resticPath version) -join "`n"
}

$disks = @(Get-Disk | Sort-Object Number | ForEach-Object {
    $disk = $_
    $partitions = @()
    if (-not $disk.IsOffline) {
        $partitions = @(Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue | ForEach-Object {
            [ordered]@{
                partition_number = $_.PartitionNumber
                drive_letter = if ($_.DriveLetter) { [string]$_.DriveLetter } else { $null }
                size_bytes = $_.Size
            }
        })
    }
    [ordered]@{
        number = $disk.Number
        friendly_name = $disk.FriendlyName
        serial_number = ([string]$disk.SerialNumber).Trim()
        unique_id = ([string]$disk.UniqueId).Trim()
        bus_type = [string]$disk.BusType
        partition_style = [string]$disk.PartitionStyle
        size_bytes = $disk.Size
        is_boot = $disk.IsBoot
        is_system = $disk.IsSystem
        is_offline = $disk.IsOffline
        is_read_only = $disk.IsReadOnly
        health_status = [string]$disk.HealthStatus
        operational_status = @($disk.OperationalStatus | ForEach-Object { [string]$_ })
        partitions = $partitions
    }
})

$result = [ordered]@{
    schema_version = 1
    administrator = $true
    os = [ordered]@{
        caption = (Get-CimInstance Win32_OperatingSystem).Caption
        version = [Environment]::OSVersion.Version.ToString()
        architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    }
    restic = [ordered]@{
        status = $resticStatus
        version = $resticVersion
        path = $resticPath
    }
    disks = $disks
    next_step = 'Выберите только пустой выделенный тестовый диск; сообщите number, unique_id и букву тестового тома.'
}

$result | ConvertTo-Json -Depth 8
