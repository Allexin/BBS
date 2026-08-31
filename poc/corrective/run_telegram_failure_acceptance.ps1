$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$credentials = Join-Path $repo 'secrets\telegram.json'
$work = Join-Path $repo '.poc-work\r6-telegram-failure'
$python = Join-Path $repo '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $credentials -PathType Leaf)) {
    throw 'Dev Telegram credentials are missing.'
}

Write-Output '[1/2] Creating a synthetic failed run in an isolated Dev outbox.'
& $python (Join-Path $PSScriptRoot 'telegram_failure_acceptance.py') `
    --credentials $credentials --work $work
if ($LASTEXITCODE -ne 0) {
    throw "Telegram failure acceptance failed with exit code $LASTEXITCODE"
}
Write-Output '[2/2] Failed-run Telegram notification delivered and outbox state verified.'
