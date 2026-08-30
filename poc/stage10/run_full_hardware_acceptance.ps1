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
$resultRoot = Join-Path $projectRoot '.poc-work\stage10'
$logPath = Join-Path $resultRoot 'full-hardware-acceptance.log'
$resultPath = Join-Path $resultRoot 'full-hardware-acceptance-result.json'
New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null

$partition = Get-Partition -DriveLetter $TestDrive -ErrorAction Stop
$disk = Get-Disk -Number $partition.DiskNumber -ErrorAction Stop
if ($disk.IsBoot -or $disk.IsSystem) {
    throw 'Boot and system disks are forbidden.'
}
if ($disk.IsOffline -or $disk.IsReadOnly) {
    throw 'Disposable drive D must initially be online and writable.'
}

$steps = @(
    @{ Name = 'Stage 0 storage and fault probes'; Script = 'poc\stage0\run_hardware_acceptance.ps1'; Arguments = @('-TestDrive', $TestDrive) },
    @{ Name = 'Stage 6 mirror'; Script = 'poc\stage6\run_mirror_acceptance.ps1'; Arguments = @('-TestDrive', $TestDrive) },
    @{ Name = 'Stage 7 snapshot'; Script = 'poc\stage7\run_snapshot_acceptance.ps1'; Arguments = @() },
    @{ Name = 'Stage 8 restore'; Script = 'poc\stage8\run_restore_acceptance.ps1'; Arguments = @() },
    @{ Name = 'Stage 9 service lifecycle'; Script = 'poc\stage9\run_service_acceptance.ps1'; Arguments = @() }
)

$startedAt = [DateTimeOffset]::UtcNow
$completed = [System.Collections.Generic.List[string]]::new()
$status = 'failed'
$failure = $null
Start-Transcript -LiteralPath $logPath -Force | Out-Null
try {
    Write-Output "Stage 10 hardware acceptance is restricted to physical disk $($disk.Number), drive $TestDrive."
    for ($index = 0; $index -lt $steps.Count; $index++) {
        $step = $steps[$index]
        Write-Output "[$($index + 1)/$($steps.Count)] Starting: $($step.Name)"
        $scriptPath = Join-Path $projectRoot $step.Script
        $processArguments = @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $scriptPath + '"')
        ) + @($step.Arguments)
        $process = Start-Process powershell.exe -ArgumentList $processArguments -NoNewWindow -PassThru
        $stepStartedAt = [DateTimeOffset]::UtcNow
        while (-not $process.WaitForExit(10000)) {
            $elapsed = [int]([DateTimeOffset]::UtcNow - $stepStartedAt).TotalSeconds
            Write-Output "[$($index + 1)/$($steps.Count)] Still running: $($step.Name) ($elapsed seconds)"
        }
        # Windows PowerShell 5.1 may not populate ExitCode after the timed
        # WaitForExit overload until the parameterless overload completes.
        $process.WaitForExit()
        $process.Refresh()
        $stepExitCode = $process.ExitCode
        if ($null -eq $stepExitCode) {
            throw "$($step.Name) finished without an observable exit code."
        }
        if ($stepExitCode -ne 0) {
            throw "$($step.Name) failed with exit code $stepExitCode."
        }
        $completed.Add([string]$step.Name)
        Write-Output "[$($index + 1)/$($steps.Count)] Passed: $($step.Name)"
    }
    $status = 'passed'
}
catch {
    $failure = $_.Exception.Message
    Write-Error $failure
}
finally {
    Stop-Transcript | Out-Null
    $result = [ordered]@{
        schema_version = 1
        status = $status
        test_drive = $TestDrive
        disk_number = $disk.Number
        started_at = $startedAt.ToString('o')
        finished_at = [DateTimeOffset]::UtcNow.ToString('o')
        completed_steps = @($completed)
        error = $failure
        log_path = $logPath
    }
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultPath -Encoding UTF8
}

Write-Output "Result saved to: $resultPath"
Write-Output "Log saved to: $logPath"
if ($status -ne 'passed') { exit 1 }
