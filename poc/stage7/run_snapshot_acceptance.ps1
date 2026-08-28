$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$resultPath = Join-Path $projectRoot '.poc-work\stage7\snapshot-hardware-result.json'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

try {
    $drive = Get-Volume -DriveLetter D -ErrorAction Stop
    if ($drive.DriveType -eq 'Fixed' -and $drive.HealthStatus -ne 'Healthy') {
        throw 'Disposable drive D is not healthy.'
    }
    if (-not (Test-Path $pythonPath -PathType Leaf)) {
        throw 'Project virtual environment Python is missing.'
    }
    & $pythonPath (Join-Path $PSScriptRoot 'snapshot_acceptance.py') --drive D
    if ($LASTEXITCODE -ne 0) {
        throw "Stage 7 Python acceptance exited with code $LASTEXITCODE."
    }
    Write-Output "Result saved to: $resultPath"
}
catch {
    New-Item -ItemType Directory -Force (Split-Path $resultPath) | Out-Null
    if (-not (Test-Path $resultPath)) {
        @{ status = 'failed'; error = $_.Exception.GetType().Name } |
            ConvertTo-Json | Set-Content -Encoding ascii $resultPath
    }
    Write-Error "Stage 7 acceptance failed. See $resultPath"
    exit 1
}
