param(
    [Parameter(Mandatory = $true)]
    [string]$Stable,

    [string]$Service = 'BBS'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repo '.venv\Scripts\python.exe'
$nssm = (Get-Command nssm.exe -CommandType Application -ErrorAction Stop).Source
$output = Join-Path $repo '.poc-work\corrective-service-install\service-result.json'

& $python (Join-Path $PSScriptRoot 'install_stable_service.py') `
    --stable $Stable --nssm $nssm --service $Service --output $output
if ($LASTEXITCODE -ne 0) {
    throw "Service installation step failed with exit code $LASTEXITCODE"
}
