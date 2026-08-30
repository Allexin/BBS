$ErrorActionPreference = 'Stop'

$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$workRoot = Join-Path $repo '.poc-work\corrective-acl'
$output = Join-Path $workRoot 'acl-result.json'
New-Item -ItemType Directory -Force -Path $workRoot | Out-Null

& (Join-Path $repo '.venv\Scripts\python.exe') `
    (Join-Path $PSScriptRoot 'acl_acceptance.py') `
    --account $current `
    --work-root $workRoot `
    --output $output
if ($LASTEXITCODE -ne 0) {
    throw "ACL acceptance failed with exit code $LASTEXITCODE"
}
