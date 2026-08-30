param(
    [Parameter(Mandatory = $true)]
    [string]$Stable,

    [Parameter(Mandatory = $true)]
    [string]$NginxAccount,

    [string]$Service = 'BBS'
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
$uvCommand = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $uvCommand) {
    throw 'uv.exe is not available in PATH. Install the approved uv build before updating Stable.'
}
$uv = $uvCommand.Source

& $python -m backup_system.deployment.deploy `
    --source $repo `
    --stable $Stable `
    --service $Service `
    --nginx-account $NginxAccount `
    --nssm $nssm `
    --uv $uv

if ($LASTEXITCODE -ne 0) {
    throw "Stable update failed with exit code $LASTEXITCODE"
}
