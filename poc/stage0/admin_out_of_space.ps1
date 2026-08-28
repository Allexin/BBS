[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-DiskPart {
    param([string[]]$Commands)
    $Commands | Set-Content -LiteralPath $script:DiskPartScript -Encoding ASCII
    $output = & diskpart.exe /s $script:DiskPartScript 2>&1
    if ($LASTEXITCODE -ne 0 -or ($output -join "`n") -match 'DiskPart has encountered an error') {
        throw "diskpart failed:`n$($output -join "`n")"
    }
}

if (-not (Test-IsAdministrator)) {
    throw 'Run PowerShell as Administrator.'
}
if (Test-Path -LiteralPath 'R:\') {
    throw 'Drive letter R is already in use.'
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$fixtureRoot = 'C:\BBSStage0Faults'
$vhdPath = Join-Path $fixtureRoot 'out-of-space.vhdx'
$sourcePath = Join-Path $fixtureRoot 'random-source.bin'
$script:DiskPartScript = Join-Path $fixtureRoot 'diskpart.txt'
$resultPath = Join-Path $projectRoot '.poc-work\stage0\out-of-space-result.json'
$lock = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'restic.lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$resticPath = Join-Path $projectRoot ".tools\restic-$($lock.version)\restic_$($lock.version)_windows_amd64.exe"

if ([IO.Path]::GetFullPath($fixtureRoot).TrimEnd('\') -ne 'C:\BBSStage0Faults') {
    throw 'Unsafe fixture path.'
}
if (Test-Path -LiteralPath $fixtureRoot) {
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $resultPath) -Force | Out-Null

$actualHash = (Get-FileHash -LiteralPath $resticPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $lock.executable_sha256) {
    throw 'restic.exe SHA-256 mismatch.'
}

$attached = $false
$result = [ordered]@{
    schema_version = 1
    status = 'failed'
    vhd_size_mib = 96
}
try {
    Invoke-DiskPart @(
        "create vdisk file=`"$vhdPath`" maximum=96 type=expandable",
        "select vdisk file=`"$vhdPath`"",
        'attach vdisk',
        'create partition primary',
        'format fs=ntfs quick label=BBS_STAGE0_FULL',
        'assign letter=R'
    )
    $attached = $true
    if (-not (Test-Path -LiteralPath 'R:\')) {
        throw 'Temporary VHD volume R did not appear.'
    }

    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $buffer = New-Object byte[] (1024 * 1024)
    $stream = [IO.File]::Open($sourcePath, 'Create', 'Write', 'None')
    try {
        for ($index = 0; $index -lt 160; $index++) {
            $rng.GetBytes($buffer)
            $stream.Write($buffer, 0, $buffer.Length)
        }
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
        $rng.Dispose()
    }

    $repository = 'R:\repository'
    $initOutput = & $resticPath --repo $repository --insecure-no-password --no-cache init --repository-version stable 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "restic init failed:`n$($initOutput -join "`n")"
    }

    $helperPath = Join-Path $PSScriptRoot 'restic_out_of_space.py'
    $helperOutput = @(& python $helperPath --restic $resticPath --repository $repository --source $sourcePath 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "out-of-space helper failed:`n$($helperOutput -join "`n")"
    }
    $probe = ($helperOutput -join "`n") | ConvertFrom-Json
    $result.status = 'passed'
    $result.restic_exit_code = $probe.restic_exit_code
    $result.structured_error_events = $probe.structured_error_events_before_interrupt
    $result.diagnostic_stream = $probe.diagnostic_stream
    $result.pinned_diagnostic_matched = $probe.pinned_diagnostic_matched
    $result.cooperative_interrupt_sent = $probe.cooperative_interrupt_sent
    $result.seconds_to_classification = $probe.seconds_to_classification
} catch {
    $result.error = $_.Exception.Message
    throw
} finally {
    if ($attached -or (Test-Path -LiteralPath $vhdPath)) {
        try {
            Invoke-DiskPart @(
                "select vdisk file=`"$vhdPath`"",
                'detach vdisk'
            )
        } catch {
            $result.cleanup_error = $_.Exception.Message
        }
    }
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resultPath -Encoding UTF8
    if (Test-Path -LiteralPath $fixtureRoot) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Output "Result saved to: $resultPath"
