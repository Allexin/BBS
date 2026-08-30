$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$nssm = Join-Path $repo '.tools\nssm\nssm.exe'
$output = Join-Path $repo '.poc-work\stage9\service-result.json'
if (-not (Test-Path -LiteralPath $nssm -PathType Leaf)) {
    throw "NSSM is missing at $nssm"
}
& python (Join-Path $PSScriptRoot 'service_acceptance.py') --nssm $nssm --output $output
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
