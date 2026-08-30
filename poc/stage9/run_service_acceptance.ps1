$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$output = Join-Path $repo '.poc-work\stage9\service-result.json'
$nssmCommand = Get-Command nssm.exe -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $nssmCommand) {
    throw 'nssm.exe is not available in the elevated process PATH'
}
$nssm = $nssmCommand.Source
& python (Join-Path $PSScriptRoot 'service_acceptance.py') --nssm $nssm --output $output
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
