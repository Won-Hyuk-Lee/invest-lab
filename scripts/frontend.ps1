$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
$nodePath = if ($nodeCommand) { $nodeCommand.Source } else { $null }
function Test-NodeVersion([string] $candidate) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { return $false }
    $detected = [version] ((& $candidate --version).Trim().TrimStart('v'))
    return $detected -ge [version] '22.12.0'
}
if (-not (Test-NodeVersion $nodePath)) {
    # Use an existing Codex runtime only when the machine's Node is too old.
    $bundledNode = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
    if (Test-NodeVersion $bundledNode) { $nodePath = $bundledNode }
    else { throw 'Node.js 22.12 or newer is required.' }
}
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) { throw 'npm is required. Install Node.js with npm.' }
$npmCli = Join-Path (Split-Path -Parent $npmCommand.Source) 'node_modules\npm\bin\npm-cli.js'
if (-not (Test-Path -LiteralPath $npmCli)) { throw 'Could not find npm-cli.js.' }
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
