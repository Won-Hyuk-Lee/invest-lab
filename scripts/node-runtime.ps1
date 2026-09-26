function Get-InvestNodeRuntime {
    function Test-InvestNodeVersion([string] $candidate) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { return $false }
        $detected = [version] ((& $candidate --version).Trim().TrimStart('v'))
        return (($detected.Major -eq 22 -and $detected -ge [version] '22.22.2') -or
                ($detected.Major -eq 24 -and $detected -ge [version] '24.15.0') -or
                $detected.Major -ge 26)
    }
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    $nodePath = if ($nodeCommand) { $nodeCommand.Source } else { $null }
    if (-not (Test-InvestNodeVersion $nodePath)) {
        $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
        if (Test-InvestNodeVersion $bundled) { $nodePath = $bundled }
        else { throw 'Node.js 24.15+ (or 22.22.2+) is required.' }
    }
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) { throw 'npm is required. Install Node.js with npm.' }
    $npmCli = Join-Path (Split-Path -Parent $npmCommand.Source) 'node_modules\npm\bin\npm-cli.js'
    if (-not (Test-Path -LiteralPath $npmCli)) { throw 'Could not find npm-cli.js.' }
    return @{ Node = $nodePath; Npm = $npmCli }
}
