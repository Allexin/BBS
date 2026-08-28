$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$resultPath = Join-Path $projectRoot '.poc-work\stage8\restore-hardware-result.json'

try {
    Get-Volume -DriveLetter D -ErrorAction Stop | Out-Null
    if (-not (Test-Path $pythonPath -PathType Leaf)) {
        throw 'Project virtual environment Python is missing.'
    }
    & $pythonPath (Join-Path $PSScriptRoot 'restore_acceptance.py') --drive D
    if ($LASTEXITCODE -ne 0) {
        throw "Stage 8 Python acceptance exited with code $LASTEXITCODE."
    }
    Write-Output "Result saved to: $resultPath"
}
catch {
    Write-Error "Stage 8 acceptance failed. See $resultPath"
    exit 1
}
