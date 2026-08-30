$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$credentials = Join-Path $repo 'secrets\telegram.json'
$result = Join-Path $repo '.poc-work\corrective-telegram\telegram-result.json'

& (Join-Path $repo '.venv\Scripts\python.exe') `
    (Join-Path $PSScriptRoot 'telegram_acceptance.py') `
    --credentials $credentials `
    --result $result
if ($LASTEXITCODE -ne 0) {
    throw "Telegram acceptance failed with exit code $LASTEXITCODE"
}
