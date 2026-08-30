param(
    [string]$Stable = 'S:\BasovBackupSystem\BBS_Stable'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

try {
    Write-Output '[1/3] Resolving drive D to one physical disk.'
    $partition = Get-Partition -DriveLetter D -ErrorAction Stop
    $disk = $partition | Get-Disk -ErrorAction Stop
    if (@($disk).Count -ne 1 -or $disk.OperationalStatus -notcontains 'Online') {
        throw 'Drive D must resolve to one online test disk.'
    }
    $serial = ([string]$disk.SerialNumber).Trim()
    if ([string]::IsNullOrWhiteSpace($serial)) {
        throw 'Test disk serial is unavailable.'
    }
    Write-Output '[2/3] Writing the test-only SMART allowlist and short-test cron job.'
    $device = '/dev/pd' + [string]$disk.Number
    $fireAt = (Get-Date).AddMinutes(5)
    $cron = '{0} {1} * * *' -f $fireAt.Minute, $fireAt.Hour
    & $pythonPath (Join-Path $PSScriptRoot 'configure_stable_smart_job.py') --stable $Stable --device $device --serial $serial --size ([string]$disk.Size) --cron $cron
    if ($LASTEXITCODE -ne 0) {
        throw "Stable SMART setup exited with code $LASTEXITCODE."
    }
    Write-Output '[3/3] Configuration validation passed.'
    Write-Output ("The test job is scheduled for approximately {0}." -f $fireAt.ToString('yyyy-MM-dd HH:mm'))
    Write-Output "Restart service 'BBS' once to apply the new configuration."
}
catch {
    Write-Error $_
    exit 1
}
