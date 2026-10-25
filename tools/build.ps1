[CmdletBinding()]
param(
    [string]$Python,
    [string]$ISCC,
    [switch]$Development,
    [switch]$Quick,
    [string[]]$TestTargets = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
$previousUserBase = [Environment]::GetEnvironmentVariable('PYTHONUSERBASE', 'Process')
$previousNoUserSite = [Environment]::GetEnvironmentVariable('PYTHONNOUSERSITE', 'Process')
$previousBuildCache = [Environment]::GetEnvironmentVariable('PYINSTALLER_CONFIG_DIR', 'Process')
$qaEnvironment = @{}
foreach ($name in @('LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP')) {
    $qaEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$buildLog = $null

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    # Native build tools commonly write progress to stderr. Log both streams,
    # then judge the native exit code rather than PowerShell's stderr wrapper.
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Program @Arguments 2>&1 | ForEach-Object {
            $line = [string]$_
            if ($buildLog) { Add-Content -LiteralPath $buildLog -Value $line -Encoding UTF8 -ErrorAction Stop }
            Write-Host $line
        }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($exitCode -ne 0) {
        throw "Команда завершилась с кодом ${exitCode}: $Program"
    }
}

function Find-InnoCompiler {
    param([string]$Requested)
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) {
            throw "Компилятор Inno Setup не найден: $Requested"
        }
        return [IO.Path]::GetFullPath($Requested)
    }
    $configured = [Environment]::GetEnvironmentVariable('ISCC_EXE', 'Process')
    if ($configured) { return Find-InnoCompiler -Requested $configured }
    $command = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @((Join-Path $repository '.tools\innosetup\ISCC.exe'))
    foreach ($base in @([Environment]::GetEnvironmentVariable('ProgramFiles'),
                         [Environment]::GetEnvironmentVariable('ProgramFiles(x86)'),
                         [Environment]::GetEnvironmentVariable('LOCALAPPDATA'))) {
        if ($base) {
            $candidates += Join-Path $base 'Inno Setup 7\ISCC.exe'
            $candidates += Join-Path $base 'Inno Setup 6\ISCC.exe'
            $candidates += Join-Path $base 'Programs\Inno Setup 7\ISCC.exe'
            $candidates += Join-Path $base 'Programs\Inno Setup 6\ISCC.exe'
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    throw 'Не найден ISCC.exe. Подготовьте Inno Setup отдельно и укажите -ISCC или ISCC_EXE. Сборка ничего не загружает и не устанавливает.'
}

Push-Location -LiteralPath $repository
try {
    if (-not $Python) { $Python = Join-Path $repository '.venv\Scripts\python.exe' }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw 'Не найден Python для сборки. Подготовьте .venv с зафиксированными зависимостями или укажите -Python.'
    }
    $Python = [IO.Path]::GetFullPath($Python)
    # Validate the mode and every target before reserving output or building a wheel.
    $helper = Join-Path $repository 'packaging\build_support.py'
    $selectionArguments = @($helper, 'test-selection', '--root', $repository)
    if ($Development) { $selectionArguments += '--development' }
    if ($Quick) { $selectionArguments += '--quick' }
    foreach ($target in $TestTargets) { $selectionArguments += "--test-target=$target" }
    $selectionOutput = & $Python @selectionArguments
    if ($LASTEXITCODE -ne 0) { throw 'Invalid build mode or TestTargets.' }
    $selection = ([string]$selectionOutput) | ConvertFrom-Json
    $scopeArguments = @()
    if ($Quick) { $scopeArguments += '--quick' }
    foreach ($target in $selection.test_targets) { $scopeArguments += "--test-target=$target" }
    $compiler = Find-InnoCompiler -Requested $ISCC
    $env:PYTHONPATH = (Join-Path $repository 'src') + [IO.Path]::PathSeparator +
        (Join-Path $repository 'packages\atom-search-core\src')
    $env:PYTHONUSERBASE = Join-Path $repository '.tools\python-userbase'
    $env:PYTHONNOUSERSITE = '1'
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $repository '.tools\pyinstaller-cache'
    $helper = Join-Path $repository 'packaging\build_support.py'
    $artifactFolder = Join-Path $repository 'artifacts'
    $distRoot = Join-Path $repository 'dist'
    $buildFolder = Join-Path $repository 'build'
    $coreWheelFolder = Join-Path $artifactFolder 'core-wheel'
    $qaFolder = $artifactFolder
    if ($Development) {
        # Checks clean HEAD before reserving the directory. An existing attempt,
        # including a completed manifest, is never reused or recursively deleted.
        $layoutOutput = & $Python $helper development-start --root $repository @scopeArguments
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить development-сборку.' }
        $layout = ([string]$layoutOutput) | ConvertFrom-Json
        $sourceCommit = Split-Path -Leaf $layout.base
        $artifactFolder = $layout.artifacts
        $distRoot = $layout.dist
        $buildFolder = $layout.build
        $coreWheelFolder = $layout.wheel
        $qaFolder = $layout.qa
        New-Item -ItemType Directory -Path $qaFolder -Force | Out-Null
        # Confine launcher fallback logs and native/compiler temporary files.
        foreach ($name in @('LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP')) {
            $folder = Join-Path $qaFolder $name.ToLowerInvariant()
            New-Item -ItemType Directory -Path $folder -Force | Out-Null
            [Environment]::SetEnvironmentVariable($name, $folder, 'Process')
        }
        # Keep PyInstaller cache beside its work files; the explicit --data-dir
        # below keeps smoke runs away from user profiles.
        $env:PYINSTALLER_CONFIG_DIR = Join-Path $buildFolder 'pyinstaller-cache'
    }
    New-Item -ItemType Directory -Path $qaFolder -Force | Out-Null
    $buildLog = Join-Path $qaFolder 'build.log'
    Set-Content -LiteralPath $buildLog -Value 'ssearch offline build' -Encoding UTF8
    $coreWheelOutput = & $Python $helper core-wheel --root $repository --directory $coreWheelFolder 2> (Join-Path $qaFolder 'core-wheel.stderr.log')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось собрать локальный wheel ядра.' }
    $coreWheel = ([string]$coreWheelOutput).Trim()
    Add-Content -LiteralPath $buildLog -Value $coreWheel -Encoding UTF8
    if (-not (Test-Path -LiteralPath $coreWheel -PathType Leaf)) {
        throw "Сборщик не создал ожидаемый wheel ядра: $coreWheel"
    }
    Invoke-Checked $Python @('-m', 'pip', '--disable-pip-version-check', 'install',
                            '--no-deps', '--no-index', '--force-reinstall', $coreWheel)
    Invoke-Checked $Python @($helper, 'preflight', '--root', $repository, '--check-dependencies')
    Invoke-Checked $Python (@('-m', 'pytest', '-q') + @($selection.test_targets))
    if ($Development) {
        # Invoke-Checked throws on every nonzero pytest exit (including no tests).
        Invoke-Checked $Python (@($helper, 'test-receipt', '--root', $repository,
                                  '--commit', $sourceCommit, '--exit-code', '0') + $scopeArguments)
    }
    $version = (& $Python $helper version --root $repository).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось определить версию приложения.' }
    $distFolder = Join-Path $distRoot 'ssearch'
    New-Item -ItemType Directory -Path $artifactFolder -Force | Out-Null
    if ($Development) {
        Invoke-Checked $Python @($helper, 'verify-source', '--root', $repository,
                                '--commit', $sourceCommit)
    }
    Invoke-Checked $Python @('-m', 'PyInstaller', '--noconfirm', '--clean',
                            '--distpath', $distRoot,
                            '--workpath', $buildFolder,
                            (Join-Path $repository 'packaging\atom.spec'))
    Invoke-Checked $Python @($helper, 'verify-bundle', '--directory', $distFolder)
    Invoke-Checked (Join-Path $distFolder 'ssearch.exe') @(
        '--postgres-self-test', (Join-Path $qaFolder 'postgres-runtime-proof.json'),
        '--data-dir', (Join-Path $qaFolder 'postgres-profile'))
    Invoke-Checked (Join-Path $distFolder 'ssearch.exe') @(
        '--self-test', (Join-Path $qaFolder 'frozen-self-test.json'),
        '--data-dir', (Join-Path $qaFolder 'self-test-data'))
    Invoke-Checked (Join-Path $distFolder 'ssearch.exe') @(
        '--smoke-test', '--no-auto-scan', '--data-dir', (Join-Path $qaFolder 'smoke-data'),
        '--screenshot', (Join-Path $qaFolder 'build-smoke.png'))
    Invoke-Checked $compiler @("/DAppVersion=$version", "/DSourceDir=$distFolder",
                              "/DOutputDir=$artifactFolder", (Join-Path $repository 'packaging\installer.iss'))
    $installer = Join-Path $artifactFolder "ssearch-Setup-$version.exe"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "Компилятор не создал ожидаемый установщик: $installer"
    }
    # Avoid PowerShell module auto-loading after redirecting QA environment.
    $installerStream = [IO.File]::OpenRead($installer)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $installerHash = [BitConverter]::ToString($hasher.ComputeHash($installerStream)).Replace('-', '').ToLowerInvariant()
        Write-Host "SHA256: $installerHash"
    } finally {
        $hasher.Dispose()
        $installerStream.Dispose()
    }
    if ($Development) {
        Invoke-Checked $Python (@($helper, 'development-manifest', '--root', $repository,
                                 '--commit', $sourceCommit) + $scopeArguments)
    }
    Write-Host "Готово: $installer"
} finally {
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONUSERBASE', $previousUserBase, 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONNOUSERSITE', $previousNoUserSite, 'Process')
    [Environment]::SetEnvironmentVariable('PYINSTALLER_CONFIG_DIR', $previousBuildCache, 'Process')
    foreach ($name in @('LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP')) {
        [Environment]::SetEnvironmentVariable($name, $qaEnvironment[$name], 'Process')
    }
    Pop-Location
}
