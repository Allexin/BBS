$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$stable = (Resolve-Path (Join-Path $repo '..\BBS_Stable')).Path
$credentials = Join-Path $repo '.poc-work\corrective-telegram\credentials.json'
$destination = Join-Path $stable 'data\config\telegram.json'
$result = Join-Path $repo '.poc-work\corrective-telegram\telegram-result.json'

& (Join-Path $repo '.venv\Scripts\python.exe') `
    (Join-Path $PSScriptRoot 'setup_telegram.py') `
    --credentials $credentials `
    --destination $destination `
    --result $result
if ($LASTEXITCODE -ne 0) {
    throw "Telegram setup failed with exit code $LASTEXITCODE"
}
