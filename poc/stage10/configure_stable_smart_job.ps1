param(
    [string]$Stable = 'S:\BasovBackupSystem\BBS_Stable',
    [string]$Service = 'BBS'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$smartctlPath = Join-Path $projectRoot '.poc-work\tools\smartmontools\bin\smartctl.exe'

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this setup from an elevated PowerShell.'
    }
    Write-Output '[1/5] Verifying that the BBS service is stopped.'
    $status = (& nssm status $Service 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot read service '$Service' status."
    }
    if ($status -ne 'SERVICE_STOPPED') {
        throw "Service '$Service' must be stopped; current status: $status"
    }
    Write-Output '[2/5] Resolving drive D to one physical disk.'
    $partition = Get-Partition -DriveLetter D -ErrorAction Stop
    $disk = $partition | Get-Disk -ErrorAction Stop
    if (@($disk).Count -ne 1 -or $disk.OperationalStatus -notcontains 'Online') {
        throw 'Drive D must resolve to one online test disk.'
    }
    $serial = ([string]$disk.SerialNumber).Trim()
    if ([string]::IsNullOrWhiteSpace($serial)) {
        throw 'Test disk serial is unavailable.'
    }
    if (-not (Test-Path $smartctlPath -PathType Leaf)) {
        throw 'Pinned Dev smartctl 7.5 is missing.'
    }
    Write-Output '[3/5] Copying pinned smartctl into Stable bin.'
    Write-Output '[4/5] Writing the test-only SMART allowlist and short-test cron job.'
    $device = '/dev/pd' + [string]$disk.Number
    $fireAt = (Get-Date).AddMinutes(10)
    $cron = '{0} {1} * * *' -f $fireAt.Minute, $fireAt.Hour
    & $pythonPath (Join-Path $PSScriptRoot 'configure_stable_smart_job.py') --stable $Stable --smartctl $smartctlPath --device $device --serial $serial --size ([string]$disk.Size) --cron $cron
    if ($LASTEXITCODE -ne 0) {
        throw "Stable SMART setup exited with code $LASTEXITCODE."
    }
    Write-Output '[5/5] Configuration validation passed.'
    Write-Output ("The test job is scheduled for approximately {0}." -f $fireAt.ToString('yyyy-MM-dd HH:mm'))
    Write-Output "Start service '$Service' when the Dev-to-Stable updater asks for it."
}
catch {
    Write-Error $_
    exit 1
}
