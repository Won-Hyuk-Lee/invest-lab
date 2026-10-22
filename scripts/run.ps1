param(
    [ValidateSet('local_simulation', 'kis_paper', 'kis_live')][string] $Mode = 'local_simulation',
    [ValidateRange(1024, 65535)][int] $Port = 8000,
    [switch] $SkipSetup
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot 'backend'
$state = if ($env:INVEST_STATE_DIR) { $env:INVEST_STATE_DIR } else { Join-Path $repoRoot '.local' }
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Stop the previous server or select another port."
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv is required.' }
$oldMode = $env:INVEST_MODE
$oldOrigin = $env:INVEST_UI_ORIGIN
$oldPath = $env:PATH
$workerProcess = $null
$env:INVEST_MODE = $Mode
$env:INVEST_UI_ORIGIN = "http://127.0.0.1:$Port"
try {
    if (-not $SkipSetup) {
        Push-Location $backend
        try {
            & uv sync --frozen
            if ($LASTEXITCODE -ne 0) { throw 'Backend installation failed.' }
        } finally { Pop-Location }
        . (Join-Path $PSScriptRoot 'node-runtime.ps1')
        $runtime = Get-InvestNodeRuntime
        $env:PATH = (Split-Path -Parent $runtime.Node) + ';' + $oldPath
        Push-Location (Join-Path $repoRoot 'frontend')
        try {
            & $runtime.Node $runtime.Npm ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'Frontend installation failed.' }
            & $runtime.Node $runtime.Npm run build
            if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
        } finally { Pop-Location }
    }
    $python = Join-Path $backend '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath (Join-Path $repoRoot 'frontend\dist\index.html'))) {
        throw 'Run without -SkipSetup for the first launch.'
    }
    $logs = Join-Path $state 'logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $runId = Get-Date -Format 'yyyyMMdd-HHmmss'
    $workerProcess = Start-Process -FilePath $python -ArgumentList @('-m', 'invest_lab.worker') `
        -WorkingDirectory $backend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs "worker-$runId.out.log") `
        -RedirectStandardError (Join-Path $logs "worker-$runId.err.log")
    Write-Host "invest-lab [$Mode] http://127.0.0.1:$Port"
    Write-Host 'Press Ctrl+C to stop this server and its worker. External orders require mode-specific settings and local controls.'
    Push-Location $backend
    try {
        & $python -m uvicorn invest_lab.adapters.http:create_app --factory --host 127.0.0.1 --port $Port
    } finally { Pop-Location }
} finally {
    if ($workerProcess -and -not $workerProcess.HasExited) {
        Stop-Process -Id $workerProcess.Id -ErrorAction SilentlyContinue
        $workerProcess.WaitForExit(10000) | Out-Null
    }
    $env:INVEST_MODE = $oldMode
    $env:INVEST_UI_ORIGIN = $oldOrigin
    $env:PATH = $oldPath
}
