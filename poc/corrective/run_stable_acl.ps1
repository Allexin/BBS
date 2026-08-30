param(
    [Parameter(Mandatory = $true)]
    [string]$Stable,

    [Parameter(Mandatory = $true)]
    [string]$NginxAccount
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repo '.venv\Scripts\python.exe'
$output = Join-Path $repo '.poc-work\corrective-stable-acl\acl-result.json'

& $python (Join-Path $PSScriptRoot 'apply_stable_acl.py') `
    --stable $Stable --nginx-account $NginxAccount --output $output
if ($LASTEXITCODE -ne 0) {
    throw "Stable ACL step failed with exit code $LASTEXITCODE"
}
