[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:GITHUB_ACTIONS -cne 'true') { throw 'CI_ACTIONS_REQUIRED' }
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$candidate = Get-Content -LiteralPath (Join-Path $repository 'frontend/compatibility/candidate-versions.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$node = (Get-Command node.exe -ErrorAction Stop).Source
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$nodeVersion = (& $node --version).Trim()
if ($LASTEXITCODE -ne 0 -or $nodeVersion -cne ('v' + $candidate.build.node)) { throw 'PINNED_NODE_REQUIRED' }
$npmVersion = (& $npm --version).Trim()
if ($LASTEXITCODE -ne 0 -or $npmVersion -cne $candidate.build.npm) { throw 'PINNED_NPM_REQUIRED' }
$globalModules = (& $npm root --global).Trim()
if ($LASTEXITCODE -ne 0) { throw 'NPM_RUNTIME_UNAVAILABLE' }
$npmSource = Join-Path $globalModules 'npm'
$npmPackage = Get-Content -LiteralPath (Join-Path $npmSource 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($npmPackage.version -cne $candidate.build.npm) { throw 'NPM_RUNTIME_VERSION_MISMATCH' }
foreach ($entry in @((Get-Item -LiteralPath $npmSource), (Get-Item -LiteralPath $node))) {
    if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'LINKED_BUILD_RUNTIME' }
}
# Check every directory before descending, rather than following a junction and
# checking just the files reached through it.
$pending = [Collections.Generic.Stack[string]]::new()
$pending.Push($npmSource)
while ($pending.Count) {
    foreach ($entry in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'LINKED_BUILD_RUNTIME' }
        if ($entry.PSIsContainer) { $pending.Push($entry.FullName) }
    }
}
$destination = Join-Path $repository '.tools/ci-node'
if (Test-Path -LiteralPath $destination) { throw 'PREPARED_NODE_ALREADY_EXISTS' }
$null = New-Item -ItemType Directory -Path $destination
Copy-Item -LiteralPath $node -Destination (Join-Path $destination 'node.exe')
Copy-Item -LiteralPath $npmSource -Destination (Join-Path $destination 'npm') -Recurse
$shim = @'
@echo off
"%~dp0node.exe" "%~dp0npm\bin\npm-cli.js" %*
'@
[IO.File]::WriteAllText((Join-Path $destination 'npm.cmd'), $shim, [Text.Encoding]::ASCII)
& (Join-Path $destination 'node.exe') (Join-Path $destination 'npm/bin/npm-cli.js') --version
if ($LASTEXITCODE -ne 0) { throw 'PREPARED_NPM_UNAVAILABLE' }
$sourceCommit = (& git -C $repository rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch '^[0-9a-f]{40}$') { throw 'SOURCE_IDENTITY_REQUIRED' }
$files = @{}
foreach ($file in Get-ChildItem -LiteralPath $destination -Recurse -File) {
    if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'LINKED_BUILD_RUNTIME' }
    $relative = $file.FullName.Substring($destination.Length + 1).Replace('\', '/')
    $files[$relative] = @{ size=$file.Length; sha256=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
}
$receipt = @{schema_version=1; source_commit=$sourceCommit; node=$nodeVersion; npm=$npmVersion; files=$files}
[IO.File]::WriteAllText((Join-Path $destination 'build-runtime.json'),
    (($receipt | ConvertTo-Json -Depth 5) + "`n"), [Text.UTF8Encoding]::new($false))
Write-Host 'Pinned portable Node/npm prepared for the admitted standard-user build.'
