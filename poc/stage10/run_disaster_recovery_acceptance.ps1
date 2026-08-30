[CmdletBinding()]
param(
    [ValidateSet('D')]
    [string]$TestDrive = 'D'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$result = Join-Path $projectRoot '.poc-work\stage10\disaster-recovery-result.json'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Project virtual environment Python is missing.'
}
$partition = Get-Partition -DriveLetter $TestDrive -ErrorAction Stop
$disk = Get-Disk -Number $partition.DiskNumber -ErrorAction Stop
if ($disk.IsBoot -or $disk.IsSystem -or $disk.IsOffline -or $disk.IsReadOnly) {
    throw 'Drive D must be an online, writable, non-system disposable disk.'
}

Write-Output '[1/3] Creating an encrypted disposable repository and snapshot on D.'
Write-Output '[2/3] Simulating total loss and rebuilding from independent recovery material.'
& $python (Join-Path $PSScriptRoot 'disaster_recovery_acceptance.py') --drive $TestDrive
if ($LASTEXITCODE -ne 0) {
    throw "Disaster recovery acceptance failed. See $result"
}
Write-Output '[3/3] Full repository check and verified restore passed without manager SQLite.'
Write-Output "Result saved to: $result"
