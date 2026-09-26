$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'node-runtime.ps1')
$runtime = Get-InvestNodeRuntime
$nodePath = $runtime.Node
$npmCli = $runtime.Npm
$previousPath = $env:PATH
$env:PATH = (Split-Path -Parent $nodePath) + ';' + $env:PATH
Push-Location (Join-Path $repoRoot 'frontend')
try {
    & $nodePath $npmCli ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    & $nodePath 'node_modules\vite\bin\vite.js' --host 127.0.0.1 --port 5173 --strictPort
    if ($LASTEXITCODE -ne 0) { throw 'Frontend stopped with an error. Check port 5173.' }
} finally {
    Pop-Location
    $env:PATH = $previousPath
}
