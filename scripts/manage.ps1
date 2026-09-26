$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repoRoot 'backend')
try {
    & uv run --frozen python -m invest_lab.cli @args
    if ($LASTEXITCODE -ne 0) { throw 'Operation failed. Review the safe diagnostic above.' }
} finally { Pop-Location }
