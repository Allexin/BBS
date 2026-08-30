param(
    [Parameter(Mandatory = $true)]
    [string]$Stable,

    [string]$Service = 'BBS'
)

$ErrorActionPreference = 'Stop'
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
    '-m', 'backup_system.deployment.update',
    '--source', $repo,
    '--stable', $Stable,
    '--service', $Service,
    '--nssm', $nssm,
    '--uv', $uv
)

& $python @deployArguments

if ($LASTEXITCODE -ne 0) {
    throw "Stable update failed with exit code $LASTEXITCODE"
}
