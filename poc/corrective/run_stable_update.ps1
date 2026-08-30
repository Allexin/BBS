param(
    [Parameter(Mandatory = $true)]
    [string]$Stable,

    [Parameter(Mandatory = $true)]
    [string]$NginxAccount,

    [string]$Service = 'BBS',

    [switch]$Initialize
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$adminRole = [Security.Principal.WindowsBuiltInRole]::Administrator
if (-not $principal.IsInRole($adminRole)) {
    throw 'Run this script from an elevated PowerShell.'
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repo '.venv\Scripts\python.exe'
$nssm = (Get-Command nssm.exe -CommandType Application -ErrorAction Stop).Source
$devUv = Join-Path $repo '.venv\Scripts\uv.exe'
$toolUv = Join-Path $repo '.poc-work\tools\uv\uv.exe'
if (Test-Path -LiteralPath $devUv -PathType Leaf) {
    $uv = $devUv
} elseif (Test-Path -LiteralPath $toolUv -PathType Leaf) {
    $uv = $toolUv
} else {
    $uvCommand = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $uvCommand) {
        $uv = $uvCommand.Source
    }
}
if ([string]::IsNullOrWhiteSpace($uv)) {
    throw 'Approved uv.exe was not found in Dev, .poc-work\tools\uv, or PATH.'
}

$deployArguments = @(
    '-m', 'backup_system.deployment.deploy',
    '--source', $repo,
    '--stable', $Stable,
    '--service', $Service,
    '--nginx-account', $NginxAccount,
    '--nssm', $nssm,
    '--uv', $uv
)
if ($Initialize) {
    $deployArguments += '--initialize'
}

& $python @deployArguments

if ($LASTEXITCODE -ne 0) {
    throw "Stable update failed with exit code $LASTEXITCODE"
}
