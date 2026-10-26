[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$NodeDirectory,
    [Parameter(Mandatory=$true)][string]$FrontendCachePath,
    [Parameter(Mandatory=$true)][string]$DesktopCachePath,
    [Parameter(Mandatory=$true)][string]$ElectronZipDirectory,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ElectronZipSha256,
    [Parameter(Mandatory=$true)][string]$ServiceStageDirectory,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [switch]$Development,
    [switch]$Quick,
    [string[]]$TestTargets = @(),
    [string[]]$FrontendTestTargets = @(),
    [string[]]$DesktopTestTargets = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$previousPath = $env:PATH
$savedEnvironment = @{}
foreach ($name in @('SSEARCH_PRODUCT_METADATA', 'SSEARCH_ELECTRON_ZIP_DIR', 'SSEARCH_ELECTRON_ZIP_SHA256',
                    'SSEARCH_SERVICE_STAGE_DIR')) {
    $savedEnvironment[$name] = @{
        present = Test-Path -LiteralPath "Env:\$name"
        value = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
}

function Invoke-DesktopChecked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build command failed.' }
}

Push-Location -LiteralPath $repository
try {
    $Python = [IO.Path]::GetFullPath($Python)
    $NodeDirectory = [IO.Path]::GetFullPath($NodeDirectory)
    $node = Join-Path $NodeDirectory 'node.exe'
    $npm = Join-Path $NodeDirectory 'npm.cmd'
    foreach ($required in @($Python, $node, $npm)) {
        if (!(Test-Path -LiteralPath $required -PathType Leaf)) { throw 'Prepared build runtime is missing.' }
    }
    # This prepared directory includes the pinned npm tree and its command shim.
    # The standard user's profile, token and KnownFolders remain authoritative.
    $env:PATH = $NodeDirectory + [IO.Path]::PathSeparator + $previousPath
    $nodeVersion = (& $node --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $nodeVersion -cne 'v24.22.1') { throw 'Pinned Node runtime is required.' }
    $npmVersion = (& $npm --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $npmVersion -cne '11.19.1') { throw 'Pinned npm runtime is required.' }
    $ElectronZipDirectory = [IO.Path]::GetFullPath($ElectronZipDirectory)
    if (!(Test-Path -LiteralPath (Join-Path $ElectronZipDirectory 'electron-v45.0.0-win32-x64.zip') -PathType Leaf)) {
        throw 'Prepared Electron ZIP is missing; this build does not acquire it.'
    }
    $env:SSEARCH_ELECTRON_ZIP_DIR = $ElectronZipDirectory
    # The caller supplies the independently pinned preparation digest. Computing
    # a digest of an untrusted input here would not establish its provenance.
    $env:SSEARCH_ELECTRON_ZIP_SHA256 = $ElectronZipSha256
    $helper = Join-Path $repository 'packaging/build_support.py'
    $selectionArguments = @($helper, 'desktop-test-selection', '--root', $repository)
    if ($Development) { $selectionArguments += '--development' }
    if ($Quick) { $selectionArguments += '--quick' }
    foreach ($target in $TestTargets) { $selectionArguments += "--test-target=$target" }
    foreach ($target in $FrontendTestTargets) { $selectionArguments += "--frontend-test-target=$target" }
    foreach ($target in $DesktopTestTargets) { $selectionArguments += "--desktop-test-target=$target" }
    $selectionOutput = & $Python @selectionArguments
    if ($LASTEXITCODE -ne 0) { throw 'Invalid combined build selection.' }
    $selection = ([string]$selectionOutput) | ConvertFrom-Json
    # Python is owned by the common harness. This component never invokes
    # pytest, including when UI-only Quick deliberately selected no Python.
    $sourceCommit = (& $Python $helper verify-source --root $repository).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Clean source is required.' }
    $ServiceStageDirectory = [IO.Path]::GetFullPath($ServiceStageDirectory)
    Invoke-DesktopChecked $Python @($helper, 'verify-service-bundle', '--directory', $ServiceStageDirectory)
    $env:SSEARCH_SERVICE_STAGE_DIR = $ServiceStageDirectory
    $OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
    if (Test-Path -LiteralPath $OutputDirectory) { throw 'Desktop output already exists; use a fresh build directory.' }
    $null = New-Item -ItemType Directory -Path $OutputDirectory
    $env:SSEARCH_PRODUCT_METADATA = Join-Path $OutputDirectory 'product-metadata.json'
    Invoke-DesktopChecked $Python @($helper, 'export-product', '--root', $repository,
                                     '--output', $env:SSEARCH_PRODUCT_METADATA)
    & (Join-Path $repository 'tools/check_offline_caches.ps1') -DesktopCachePath $DesktopCachePath
    if (!$?) { throw 'Real offline cache-negative proof failed.' }
    & (Join-Path $repository 'tools/build_frontend.ps1') -CachePath $FrontendCachePath -DesktopCachePath $DesktopCachePath
    if (!$?) { throw 'Offline frontend build failed.' }
    Push-Location -LiteralPath (Join-Path $repository 'frontend')
    try {
        Invoke-DesktopChecked $npm @('run', 'typecheck')
        if (!$Quick) { Invoke-DesktopChecked $npm @('test') }
        elseif (@($selection.frontend_test_targets).Count) {
            Invoke-DesktopChecked $npm (@('test', '--') + @($selection.frontend_test_targets))
        }
    } finally { Pop-Location }
    Push-Location -LiteralPath (Join-Path $repository 'desktop')
    try {
        Invoke-DesktopChecked $npm @('run', 'typecheck')
        if (!$Quick) { Invoke-DesktopChecked $npm @('test') }
        elseif (@($selection.desktop_test_targets).Count) {
            Invoke-DesktopChecked $node (@('node_modules/tsx/dist/cli.mjs', '--test') + @($selection.desktop_test_targets))
        }
        Invoke-DesktopChecked $npm @('run', 'build')
        # Forge 8.0.1 CLI has no outDir flag; its package API accepts one.
        # Never call make/publish or download dependencies during this build.
        $packageIndex = Join-Path $OutputDirectory 'package-results.json'
        $packageScript = 'require("@electron-forge/core").api.package({dir:process.cwd(),platform:"win32",arch:"x64",outDir:process.argv[1],interactive:false}).then(result=>require("node:fs").writeFileSync(process.argv[2],JSON.stringify(result),{flag:"wx"})).catch(()=>{console.error("desktop_package_failed");process.exitCode=1})'
        Invoke-DesktopChecked $node @('-e', $packageScript, $OutputDirectory, $packageIndex)
    } finally { Pop-Location }
    $packages = @(Get-Content -LiteralPath $packageIndex -Raw -Encoding UTF8 | ConvertFrom-Json)
    if ($packages.Count -ne 1 -or $packages[0].platform -cne 'win32' -or $packages[0].arch -cne 'x64') {
        throw 'Expected exactly one Windows x64 package.'
    }
    $packagedPath = [IO.Path]::GetFullPath($packages[0].packagedPath)
    if (!$packagedPath.StartsWith($OutputDirectory.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Packaged output escaped its reserved directory.'
    }
    $inventory = Join-Path $OutputDirectory 'payload-inventory.json'
    Invoke-DesktopChecked $Python @($helper, 'verify-desktop-bundle', '--root', $repository,
                                     '--directory', $packagedPath, '--commit', $sourceCommit, '--output', $inventory)
    Invoke-DesktopChecked $Python @($helper, 'verify-source', '--root', $repository, '--commit', $sourceCommit)
    $receipt = @{
        schema_version = 1; source_commit = $sourceCommit; test_scope = $selection.scope
        python_tests_delegated = @($selection.test_targets)
        frontend_test_targets = @($selection.frontend_test_targets)
        desktop_test_targets = @($selection.desktop_test_targets)
        node = $nodeVersion; npm = $npmVersion; package_only = $true
        packaged_path = $packagedPath; payload_inventory = $inventory
        frozen_acceptance = $false
    }
    [IO.File]::WriteAllText((Join-Path $OutputDirectory 'desktop-build-receipt.json'),
        (($receipt | ConvertTo-Json -Depth 5) + "`n"), [Text.UTF8Encoding]::new($false))
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        if ($savedEnvironment[$name].present) {
            [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name].value, 'Process')
        } else {
            # PowerShell can bind null as an empty string. Remove the entry to
            # preserve absence instead of leaving a present-empty variable.
            Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue
        }
    }
    $env:PATH = $previousPath
    Pop-Location
}
