$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this acceptance from an elevated PowerShell.'
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repo '.venv\Scripts\python.exe'
$helper = Join-Path $PSScriptRoot 'restic_secret_acl_acceptance.py'
$work = Join-Path $repo '.poc-work\r6-secret-acl'
$result = Join-Path $work 'result.json'
$stdout = Join-Path $work 'service.stdout.log'
$stderr = Join-Path $work 'service.stderr.log'
$service = 'BBS-R6-SecretAcl-Acceptance'

New-Item -ItemType Directory -Path $work -Force | Out-Null
Remove-Item -LiteralPath $result, $stdout, $stderr -Force -ErrorAction SilentlyContinue

Write-Output '[1/4] Installing an isolated LocalSystem acceptance service.'
& nssm install $service $python $helper --result $result | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to install the acceptance service.' }

try {
    & nssm set $service AppDirectory $repo | Out-Null
    & nssm set $service AppStdout $stdout | Out-Null
    & nssm set $service AppStderr $stderr | Out-Null
    & nssm set $service AppExit Default Exit | Out-Null

    Write-Output '[2/4] Starting the LocalSystem ACL check with a test-only secret.'
    & nssm start $service | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to start the acceptance service.' }

    Write-Output '[3/4] Waiting for the bounded acceptance result.'
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while (-not (Test-Path -LiteralPath $result -PathType Leaf)) {
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "Acceptance timed out. See $stderr"
        }
        Start-Sleep -Milliseconds 500
    }
    $payload = Get-Content -LiteralPath $result -Raw | ConvertFrom-Json
    if ($payload.result -ne 'success') {
        throw "ACL acceptance failed: $($payload.error)"
    }
    Write-Output "Result saved to: $result"
    Write-Output '[4/4] LocalSystem secret-file ACL acceptance passed.'
}
finally {
    & nssm stop $service | Out-Null
    & nssm remove $service confirm | Out-Null
}
