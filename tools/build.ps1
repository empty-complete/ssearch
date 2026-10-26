[CmdletBinding()]
param(
    [string]$Python,
    [string]$ISCC,
    [switch]$Development,
    [switch]$Quick,
    [switch]$Electron,
    [string]$NodeDirectory,
    [string]$FrontendCachePath,
    [string]$DesktopCachePath,
    [string]$ElectronZipDirectory,
    [ValidatePattern('^[0-9a-f]{64}$')][string]$ElectronZipSha256,
    [string[]]$TestTargets = @(),
    [string[]]$FrontendTestTargets = @(),
    [string[]]$DesktopTestTargets = @()
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

function Get-BuildFileSha256([string]$Path) {
    # Do not auto-load a PowerShell module after redirecting the QA environment.
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose(); $stream.Dispose() }
}

function Read-BuildJson([string]$Path) {
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -gt 65536) {
        throw 'Build receipt is not a bounded ordinary file.'
    }
    return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) | ConvertFrom-Json
}

function Invoke-OwnedProof {
    param([string]$Program, [string[]]$Arguments,
          [ValidateRange(1, 19800)][int]$TimeoutSeconds)
    # Retain only the process we create. The admitted CI job owns descendants;
    # a timeout never authorizes looking up or terminating a descriptor's PID.
    $process = [Diagnostics.Process]::new()
    $stdout = $null; $stderr = $null; $stdoutTask = $null; $stderrTask = $null
    $started = $false; $cleanupConfirmed = $true; $failure = $null
    try {
        $quoted = foreach ($argument in $Arguments) {
            # Same explicit quoting boundary as the existing installer QA helper.
            if ($argument.Contains('"') -or $argument.EndsWith('\') -or $argument -match '[\x00\r\n]') {
                throw 'FROZEN_NATIVE_ARGUMENT_INVALID'
            }
            '"' + $argument + '"'
        }
        $capture = Join-Path $qaFolder ('frozen-native-' + [Guid]::NewGuid().ToString('N'))
        # Drain both pipes asynchronously to owned private QA files. Never emit
        # their raw contents; none of these files belongs to the public export.
        $stdout = [IO.File]::Open($capture + '.stdout', 'CreateNew', 'Write', 'Read')
        $stderr = [IO.File]::Open($capture + '.stderr', 'CreateNew', 'Write', 'Read')
        $process.StartInfo.FileName = $Program
        $process.StartInfo.Arguments = $quoted -join ' '
        $process.StartInfo.WorkingDirectory = Split-Path -Parent $Program
        $process.StartInfo.UseShellExecute = $false
        $process.StartInfo.CreateNoWindow = $true
        $process.StartInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
        $process.StartInfo.RedirectStandardInput = $true
        $process.StartInfo.RedirectStandardOutput = $true
        $process.StartInfo.RedirectStandardError = $true
        Write-Host "Frozen native proof budget_seconds=$TimeoutSeconds"
        if (-not $process.Start()) { throw 'FROZEN_NATIVE_START_FAILED' }
        $started = $true
        $null = $process.Handle # Pin this exact process object before any wait.
        $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
        $stderrTask = $process.StandardError.BaseStream.CopyToAsync($stderr)
        $process.StandardInput.Close()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $failure = 'FROZEN_NATIVE_TIMEOUT'
        } elseif ($process.ExitCode -ne 0) {
            $failure = 'FROZEN_NATIVE_EXIT_' + $process.ExitCode
        }
    } catch {
        if (-not $failure) { $failure = 'FROZEN_NATIVE_FAILED' }
    } finally {
        if ($started) {
            try {
                if (-not $process.HasExited) {
                    $process.Kill()
                    if (-not $process.WaitForExit(15000)) { $cleanupConfirmed = $false }
                }
            } catch { $cleanupConfirmed = $false }
            foreach ($task in @($stdoutTask, $stderrTask)) {
                if ($null -ne $task) {
                    try { if (-not $task.Wait(10000)) { $cleanupConfirmed = $false } }
                    catch { $cleanupConfirmed = $false }
                }
            }
        }
        foreach ($stream in @($stdout, $stderr)) {
            if ($null -ne $stream) {
                try { $stream.Dispose() } catch { $cleanupConfirmed = $false }
            }
        }
        try { $process.Dispose() } catch { $cleanupConfirmed = $false }
    }
    if (-not $cleanupConfirmed) { throw 'FROZEN_NATIVE_CLEANUP_UNRESOLVED' }
    if ($failure) { throw $failure }
}

function Invoke-FrozenChecked {
    param([string]$Program, [string[]]$Arguments,
          [ValidateRange(1, 19800)][int]$TimeoutSeconds = 600)
    # Frozen children must resolve no prepared Python/Node or source bootstrap.
    # Keep the real standard-user profile and KnownFolders authoritative.
    $saved = @{}
    foreach ($name in [Environment]::GetEnvironmentVariables('Process').Keys) {
        if ($name -match '^(PYTHON|PYTEST|PYI_|_PYI_|NODE|ELECTRON|NPM_|CONDA_|PG|SSEARCH_)' -or
            $name -in @('PATH', 'VIRTUAL_ENV', '__PYVENV_LAUNCHER__', 'SOURCE_SHA', 'BUILD_MODE')) {
            $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        }
    }
    try {
        foreach ($name in $saved.Keys) { Remove-Item -LiteralPath "Env:\$name" -ErrorAction Stop }
        $env:PATH = Join-Path $env:SystemRoot 'System32'
        Invoke-OwnedProof $Program $Arguments $TimeoutSeconds
    } finally {
        foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    }
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
    if ($Electron -and (-not $Development -or -not $NodeDirectory -or -not $FrontendCachePath -or
                       -not $DesktopCachePath -or -not $ElectronZipDirectory -or -not $ElectronZipSha256)) {
        throw 'Electron requires Development and explicitly prepared Node, caches and pinned Electron ZIP.'
    }
    if (-not $Electron -and ($FrontendTestTargets.Count -or $DesktopTestTargets.Count)) {
        throw 'Frontend and desktop targets require Electron.'
    }
    if ($Electron) {
        $pins = Read-BuildJson (Join-Path $repository 'resources/ci/build-inputs.json')
        if ($ElectronZipSha256 -cne $pins.electron_runtime.archive.sha256) { throw 'Electron ZIP digest differs from the pinned input.' }
    }
    $selectionCommand = if ($Electron) { 'desktop-test-selection' } else { 'test-selection' }
    $selectionArguments = @($helper, $selectionCommand, '--root', $repository)
    if ($Development) { $selectionArguments += '--development' }
    if ($Quick) { $selectionArguments += '--quick' }
    foreach ($target in $TestTargets) { $selectionArguments += "--test-target=$target" }
    foreach ($target in $FrontendTestTargets) { $selectionArguments += "--frontend-test-target=$target" }
    foreach ($target in $DesktopTestTargets) { $selectionArguments += "--desktop-test-target=$target" }
    $selectionOutput = & $Python @selectionArguments
    if ($LASTEXITCODE -ne 0) { throw 'Invalid build mode or TestTargets.' }
    $selection = ([string]$selectionOutput) | ConvertFrom-Json
    if ($Electron) {
        $policyOutput = & $Python $helper ui-proof-policy --root $repository
        if ($LASTEXITCODE -ne 0) { throw 'Could not read the shared UI proof policy.' }
        $uiPolicy = ([string]$policyOutput) | ConvertFrom-Json
        foreach ($scenario in @('load', 'security', 'lifecycle', 'empty-profile')) {
            $budget = $uiPolicy.budgets_ms.$scenario
            if (-not ($budget -is [int] -or $budget -is [long]) -or $budget -le 0 -or $budget -gt 19680000) {
                throw 'Invalid shared UI proof budget.'
            }
        }
    }
    $scopeArguments = @()
    if ($Electron) { $scopeArguments += '--desktop-stack' }
    if ($Quick) { $scopeArguments += '--quick' }
    foreach ($target in $selection.test_targets) { $scopeArguments += "--test-target=$target" }
    if ($Electron) {
        foreach ($target in $selection.frontend_test_targets) { $scopeArguments += "--frontend-test-target=$target" }
        foreach ($target in $selection.desktop_test_targets) { $scopeArguments += "--desktop-test-target=$target" }
    }
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
        # Preserve the legacy Qt confinement. Electron proofs retain the actual
        # standard-user profile/KnownFolders and select each QA profile explicitly.
        if (-not $Electron) {
            foreach ($name in @('LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP')) {
                $folder = Join-Path $qaFolder $name.ToLowerInvariant()
                New-Item -ItemType Directory -Path $folder -Force | Out-Null
                [Environment]::SetEnvironmentVariable($name, $folder, 'Process')
            }
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
    # Empty targets mean full discovery to ci_pytest; a UI-only Quick must not
    # accidentally execute all Python tests. Full still invokes it exactly once.
    if (-not $Electron -or -not $Quick -or @($selection.test_targets).Count) {
        Invoke-Checked $Python (@('-u', (Join-Path $repository 'tools/ci_pytest.py'),
                                 '--timeout-seconds', '3600') + @($selection.test_targets))
    }
    if ($Development -and -not $Electron) {
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
    $installerArguments = @("/DAppVersion=$version", "/DOutputDir=$artifactFolder")
    if ($Electron) {
        Invoke-Checked $Python @('-m', 'PyInstaller', '--noconfirm', '--clean',
                                '--distpath', $distRoot, '--workpath', $buildFolder,
                                (Join-Path $repository 'packaging/ssearch-service.spec'))
        $serviceStage = Join-Path $distRoot 'ssearch-service'
        Invoke-Checked $Python @($helper, 'verify-service-bundle', '--directory', $serviceStage)
        $serviceHash = Get-BuildFileSha256 (Join-Path $serviceStage 'ssearch-service.exe')
        $desktopStage = Join-Path $buildFolder 'desktop-stage'
        $desktopOptions = @{
            Python = $Python; NodeDirectory = $NodeDirectory
            FrontendCachePath = $FrontendCachePath; DesktopCachePath = $DesktopCachePath
            ElectronZipDirectory = $ElectronZipDirectory; ElectronZipSha256 = $ElectronZipSha256
            ServiceStageDirectory = $serviceStage; OutputDirectory = $desktopStage
            Development = $true; Quick = [bool]$Quick
            TestTargets = @($selection.test_targets)
            FrontendTestTargets = @($selection.frontend_test_targets)
            DesktopTestTargets = @($selection.desktop_test_targets)
        }
        & (Join-Path $repository 'tools/build_desktop.ps1') @desktopOptions
        if (-not $?) { throw 'Offline desktop package failed.' }
        $desktopReceiptPath = Join-Path $desktopStage 'desktop-build-receipt.json'
        $desktopReceipt = Read-BuildJson $desktopReceiptPath
        if ($desktopReceipt.schema_version -ne 1 -or $desktopReceipt.source_commit -cne $sourceCommit -or
            $desktopReceipt.test_scope -cne $selection.scope -or $desktopReceipt.package_only -ne $true -or
            $desktopReceipt.frozen_acceptance -ne $false) { throw 'Desktop package receipt identity mismatch.' }
        foreach ($pair in @(@('python_tests_delegated', 'test_targets'),
                            @('frontend_test_targets', 'frontend_test_targets'),
                            @('desktop_test_targets', 'desktop_test_targets'))) {
            if ([string]::Join([char]0, @($desktopReceipt.($pair[0]))) -cne
                [string]::Join([char]0, @($selection.($pair[1])))) { throw 'Desktop package test selection mismatch.' }
        }
        $distFolder = [IO.Path]::GetFullPath($desktopReceipt.packaged_path)
        $stagePrefix = [IO.Path]::GetFullPath($desktopStage).TrimEnd('\') + '\'
        if (-not $distFolder.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFullPath($desktopReceipt.payload_inventory) -cne (Join-Path $desktopStage 'payload-inventory.json')) {
            throw 'Desktop package escaped its reserved stage.'
        }
        $payloadInventory = [IO.Path]::GetFullPath($desktopReceipt.payload_inventory)
        if ((Get-BuildFileSha256 (Join-Path $distFolder 'service/ssearch-service.exe')) -cne $serviceHash) {
            throw 'Forge packaged a different service image.'
        }
        # All three selected suites have now returned successfully. UI-only Quick
        # records its deliberate empty Python list without invoking full pytest.
        Invoke-Checked $Python (@($helper, 'test-receipt', '--root', $repository,
                                  '--commit', $sourceCommit, '--exit-code', '0') + $scopeArguments)
        # One full execution copy exercises both Electron and its service under
        # the same Unicode installation path. Inno consumes the clean Forge copy.
        $qaRuntime = Join-Path $qaFolder 'Frozen desktop Кириллица with spaces'
        if (Test-Path -LiteralPath $qaRuntime) { throw 'Frozen desktop QA directory already exists.' }
        Copy-Item -LiteralPath $distFolder -Destination $qaRuntime -Recurse
        $serviceExecutable = Join-Path $qaRuntime 'service/ssearch-service.exe'
        $desktopExecutable = Join-Path $qaRuntime 'ssearch.exe'
        $desktopHash = Get-BuildFileSha256 (Join-Path $distFolder 'ssearch.exe')
        if ((Get-BuildFileSha256 $serviceExecutable) -cne $serviceHash -or
            (Get-BuildFileSha256 $desktopExecutable) -cne $desktopHash) { throw 'Frozen QA image mismatch.' }
        Invoke-Checked $Python @($helper, 'verify-desktop-bundle', '--root', $repository,
            '--commit', $sourceCommit, '--directory', $qaRuntime, '--inventory', $payloadInventory)
        Invoke-Checked $Python @($helper, 'verify-service-bundle', '--directory', (Join-Path $qaRuntime 'service'))
        Invoke-FrozenChecked $serviceExecutable @(
            '--postgres-self-test', (Join-Path $qaFolder 'postgres-runtime-proof.json'),
            '--data-dir', (Join-Path $qaFolder 'Postgres profile Кириллица with spaces'))
        Invoke-FrozenChecked $serviceExecutable @(
            '--self-test', (Join-Path $qaFolder 'frozen-self-test.json'),
            '--data-dir', (Join-Path $qaFolder 'Application profile Кириллица with spaces'))
        Invoke-FrozenChecked $serviceExecutable @(
            '--service-test', (Join-Path $qaFolder 'service-owner-proof.json'),
            '--data-dir', (Join-Path $qaFolder 'Service owner profile Кириллица with spaces'))
        Invoke-Checked $Python @((Join-Path $repository 'tools/check_frozen_service.py'),
            '--service-executable', $serviceExecutable,
            '--qa-directory', (Join-Path $qaFolder 'Serve profiles Кириллица with spaces'),
            '--source-commit', $sourceCommit, '--app-version', $version,
            '--report', (Join-Path $qaFolder 'frozen-service-proof.json'))
        foreach ($scenario in @('load', 'security', 'lifecycle', 'empty-profile')) {
            $proofSeconds = [int][Math]::Ceiling($uiPolicy.budgets_ms.$scenario / 1000.0) + 120
            Invoke-FrozenChecked $desktopExecutable @('--ui-self-test', (Join-Path $qaFolder "ui-$scenario.json"),
                '--ui-scenario', $scenario, '--data-dir', (Join-Path $qaFolder "UI $scenario Кириллица with spaces")) $proofSeconds
        }
        if ((Get-BuildFileSha256 $serviceExecutable) -cne $serviceHash -or
            (Get-BuildFileSha256 (Join-Path $distFolder 'service/ssearch-service.exe')) -cne $serviceHash -or
            (Get-BuildFileSha256 (Join-Path $serviceStage 'ssearch-service.exe')) -cne $serviceHash -or
            (Get-BuildFileSha256 $desktopExecutable) -cne $desktopHash -or
            (Get-BuildFileSha256 (Join-Path $distFolder 'ssearch.exe')) -cne $desktopHash) {
            throw 'Frozen image changed during proof.'
        }
        foreach ($payload in @($qaRuntime, $distFolder)) {
            Invoke-Checked $Python @($helper, 'verify-desktop-bundle', '--root', $repository,
                '--commit', $sourceCommit, '--directory', $payload, '--inventory', $payloadInventory)
        }
        # Required common validator binds actual frozen/UI receipts before Inno.
        # Its desktop-stack implementation is a dependent P4 gate, never replaced
        # with the legacy Qt manifest or a successful process-exit assumption.
        Invoke-Checked $Python @($helper, 'verify-desktop-proofs', '--root', $repository,
            '--commit', $sourceCommit, '--directory', $distFolder, '--qa-directory', $qaFolder,
            '--desktop-receipt', $desktopReceiptPath)
        $metadataPath = Join-Path $desktopStage 'product-metadata.json'
        if ((Get-BuildFileSha256 $metadataPath) -cne
            (Get-BuildFileSha256 (Join-Path $distFolder 'resources/product-metadata.json'))) {
            throw 'Installer and Forge product metadata differ.'
        }
        $metadata = Read-BuildJson $metadataPath
        if ($metadata.schema_version -ne 1 -or $metadata.source_commit -cne $sourceCommit -or
            $metadata.app_version -cne $version) { throw 'Installer product metadata identity mismatch.' }
        foreach ($value in @($metadata.product.product_name, $metadata.product.product_name_ru,
                             $metadata.product.author_name, $metadata.product.organization_group,
                             $metadata.product.support_email, $metadata.product.copyright_holder)) {
            if (-not $value -or $value -match '["\x00-\x1f\x7f]') { throw 'Unsafe installer product metadata.' }
        }
        $installerArguments += @('/DElectronBundle=1', "/DAppName=$($metadata.product.product_name)",
            "/DAppPublisher=$($metadata.product.author_name) ($($metadata.product.organization_group))",
            "/DAppSupportEmail=$($metadata.product.support_email)",
            "/DAppDescription=$($metadata.product.product_name) — $($metadata.product.product_name_ru)",
            "/DAppCopyrightText=Copyright (c) 2026 $($metadata.product.copyright_holder) — MIT License")
    } else {
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
    }
    $installerArguments += @("/DSourceDir=$distFolder", (Join-Path $repository 'packaging\installer.iss'))
    Invoke-Checked $compiler $installerArguments
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
