$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$stable = (Resolve-Path (Join-Path $repo '..\BBS_Stable')).Path
$credentials = Join-Path $repo '.poc-work\corrective-telegram\credentials.json'
$secrets = Join-Path $stable 'data\config\secrets'
$result = Join-Path $repo '.poc-work\corrective-telegram\telegram-result.json'

& (Join-Path $repo '.venv\Scripts\python.exe') `
    (Join-Path $PSScriptRoot 'setup_telegram.py') `
    --credentials $credentials `
    --secret-directory $secrets `
    --result $result
if ($LASTEXITCODE -ne 0) {
    throw "Telegram setup failed with exit code $LASTEXITCODE"
}
