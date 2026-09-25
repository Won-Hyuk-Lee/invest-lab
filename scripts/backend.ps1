$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required. Install uv, then rerun this script.'
}
Push-Location (Join-Path $repoRoot 'backend')
try {
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
    & uv run --frozen uvicorn invest_lab.adapters.http:create_app --factory --host 127.0.0.1 --port 8000
    if ($LASTEXITCODE -ne 0) { throw 'Backend stopped with an error. Check port 8000.' }
} finally {
    Pop-Location
}
