param([switch]$Once)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repoRoot 'backend')
try {
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Worker dependency installation failed.' }
    if ($Once) {
        & uv run --frozen python -m invest_lab.worker --once
    } else {
        & uv run --frozen python -m invest_lab.worker
    }
    if ($LASTEXITCODE -ne 0) { throw 'Worker stopped with an error.' }
} finally {
    Pop-Location
}
