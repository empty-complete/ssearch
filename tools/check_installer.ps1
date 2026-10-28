[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$TestDir,
    [string]$DesktopEvidenceDirectory,
    [string]$DesktopEvidenceNonce,
    [switch]$Electron,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$artifacts = [IO.Path]::GetFullPath((Join-Path $repository 'artifacts'))
$comparison = [StringComparison]::OrdinalIgnoreCase
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1'
$runId = [Guid]::NewGuid().ToString('N')
$groupName = "ssearch QA $runId"

function Assert-ArtifactPath {
    param([string]$Path, [switch]$Qa)
    # Reject drive-relative paths (C:foo) and root-relative paths (\foo), too.
    if ($Path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+[\\/])' -or $Path.Contains('"')) {
        throw "Требуется абсолютный путь без кавычек: $Path"
    }
    $absolute = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if (-not $absolute.StartsWith($artifacts.TrimEnd('\', '/') + '\', $comparison)) {
        throw "Путь должен находиться строго внутри $artifacts : $absolute"
    }
    $relative = $absolute.Substring($artifacts.TrimEnd('\', '/').Length + 1)
    if ($relative -notmatch '^development(?:-quick)?\\[0-9a-f]{40}\\' -or
        ($Qa -and $relative -notmatch '^development(?:-quick)?\\[0-9a-f]{40}\\qa\\.+')) {
        throw 'QA requires artifacts/development[-quick]/<40-hex-SHA>/qa/<new-run>.'
    }
    $cursor = $absolute
    while ($true) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Путь QA не должен проходить через ссылку или junction: $cursor"
            }
        }
        if ($cursor.Equals($repository, $comparison)) { break }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { throw "Не удалось проверить границу рабочего каталога: $Path" }
        $cursor = $parent.FullName
    }
    return $absolute
}

function Start-HiddenAndWait {
    param([string]$Program, [string[]]$Arguments, [string]$Label,
          [ValidateRange(1, 1800000)][int]$TimeoutMilliseconds = 1800000)
    # Start-Process joins ArgumentList. Explicit quoting preserves spaces in
    # Windows PowerShell 5.1 as well as PowerShell 7; no command shell is used.
    $quoted = foreach ($argument in $Arguments) {
        if ($argument.Contains('"') -or $argument.EndsWith('\')) {
            throw "Небезопасный аргумент процесса QA: $argument"
        }
        '"' + $argument + '"'
    }
    $script:processCompletionConfirmed = $false
    $script:processCompletionReason = 'preparing_waiter'
    $script:processWaitObservation = @{waiter_finished=$false; waiter_exit_integer=$false;
        waiter_exit_zero=$false; completion_present=$false}
    $script:processCompletionPayload = $null
    # Inno's initial uninstaller exits before its temporary child finishes.
    # A separate hidden waiter preserves Start-Process -Wait's descendant-tree
    # semantics while the coordinator bounds the wait without killing anything.
    $waitId = [Guid]::NewGuid().ToString('N')
    $completionPath = Join-Path $testPath ("process-completion-$waitId.json")
    $requestValue = @{ program = $Program; arguments = ($quoted -join ' ');
        directory = $testPath; completion = $completionPath; id = $waitId } | ConvertTo-Json -Compress
    $request = $requestValue
    if (Get-Variable -Name isElectron -ValueOnly -ErrorAction SilentlyContinue) {
        $request = @{program=$Program; arguments=($quoted -join ' '); directory=$testPath;
            completion=$completionPath; id=$waitId; stdout=(Join-Path $testPath "$waitId.stdout");
            stderr=(Join-Path $testPath "$waitId.stderr");
            frozen=($Program -ine $Python -and $Program -in @($appExecutable,$serviceExecutable,
                $referenceExecutable,$referenceServiceExecutable))} | ConvertTo-Json -Compress
    }
    $encodedRequest = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($request))
    $waiterScript = @'
$ErrorActionPreference = 'Stop'
try {
    $request = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__REQUEST__')) |
        ConvertFrom-Json
    $parameters = @{FilePath=$request.program; ArgumentList=$request.arguments;
        WorkingDirectory=$request.directory; WindowStyle='Hidden'; Wait=$true; PassThru=$true}
    if ($request.PSObject.Properties['stdout']) {
        $parameters.RedirectStandardOutput=$request.stdout
        $parameters.RedirectStandardError=$request.stderr
    }
    if ($request.PSObject.Properties['frozen'] -and $request.frozen) {
        foreach ($name in @([Environment]::GetEnvironmentVariables('Process').Keys)) {
            if ($name -match '^(PYTHON|PYTEST|PYI_|_PYI_|NODE|ELECTRON|NPM_|CONDA_|PG|SSEARCH_)' -or
                $name -in @('PATH','VIRTUAL_ENV','__PYVENV_LAUNCHER__','SOURCE_SHA','BUILD_MODE')) {
                Remove-Item -LiteralPath ('Env:\' + $name)
            }
        }
        $env:PATH=Join-Path $env:SystemRoot 'System32'
    }
    $child = Start-Process @parameters
    $value = @{ id = $request.id; completed = $true; exit_code = $child.ExitCode } |
        ConvertTo-Json -Compress
    $stream = [IO.File]::Open($request.completion, [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($value)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    exit 0
} catch { exit 1 }
'@
    $waiterScript = $waiterScript.Replace('__REQUEST__', $encodedRequest)
    $encodedScript = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($waiterScript))
    $waiter = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $process = Start-Process -FilePath $waiter -ArgumentList @(
        '-NoProfile', '-NonInteractive', '-EncodedCommand', $encodedScript) `
        -WorkingDirectory $testPath -WindowStyle Hidden -PassThru
    $script:processCompletionReason = 'waiting'
    $finished = $false
    if (Get-Variable -Name isElectron -ValueOnly -ErrorAction SilentlyContinue) {
        $clock = [Diagnostics.Stopwatch]::StartNew()
        do {
            $finished = $process.WaitForExit(100)
            foreach ($name in @("$waitId.stdout", "$waitId.stderr")) {
                $capture = Join-Path $testPath $name
                if ((Test-Path -LiteralPath $capture) -and (Get-Item -LiteralPath $capture).Length -gt 1048576) {
                    $script:processCompletionReason='capture_limit'
                    throw 'INSTALLER_CHILD_CAPTURE_LIMIT'
                }
            }
        } while (-not $finished -and $clock.ElapsedMilliseconds -lt $TimeoutMilliseconds)
    } else { $finished = $process.WaitForExit($TimeoutMilliseconds) }
    if (-not $finished) {
        $script:processCompletionReason = 'waiter_timeout'
        throw "$Label timed out; descendant completion unknown, files preserved."
    }
    $script:processWaitObservation.waiter_finished = $true
    $script:processCompletionReason = 'reading_waiter_exit'
    $waiterExitCode = $process.ExitCode
    $script:processCompletionReason = 'checking_completion_file'
    $completionPresent = Test-Path -LiteralPath $completionPath -PathType Leaf
    $script:processWaitObservation = @{waiter_finished=$true;
        waiter_exit_integer=($waiterExitCode -is [int] -or $waiterExitCode -is [long]);
        waiter_exit_zero=($null -ne $waiterExitCode -and $waiterExitCode -eq 0);
        completion_present=$completionPresent}
    if ($waiterExitCode -ne 0 -or -not $completionPresent) {
        $script:processCompletionReason = if ($null -eq $waiterExitCode) { 'waiter_exit_unavailable' }
            elseif ($waiterExitCode -ne 0) { 'waiter_exit_nonzero' } else { 'completion_missing' }
        throw "$Label descendant completion unknown; files preserved."
    }
    $script:processCompletionReason = 'reading_completion'
    $completion = Get-Content -LiteralPath $completionPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $integerExit = $completion.exit_code -is [int] -or $completion.exit_code -is [long]
    $script:processCompletionPayload = @{id_matches=($completion.id -ceq $waitId);
        completed=($completion.completed -eq $true); exit_integer=$integerExit;
        exit_in_range=($integerExit -and $completion.exit_code -ge [int]::MinValue -and $completion.exit_code -le [int]::MaxValue);
        exit_zero=($integerExit -and $completion.exit_code -eq 0)}
    if ($completion.id -cne $waitId -or $completion.completed -ne $true -or
        -not ($completion.exit_code -is [int] -or $completion.exit_code -is [long]) -or
        $completion.exit_code -lt [int]::MinValue -or $completion.exit_code -gt [int]::MaxValue) {
        $script:processCompletionReason = 'completion_payload_invalid'
        throw "$Label descendant completion evidence invalid; files preserved."
    }
    # ConvertFrom-Json returns Int32 in Windows PowerShell, Int64 in PowerShell 7.
    # Coerce only after validating the producer's signed Process.ExitCode range.
    $exitCode = [int]$completion.exit_code
    $script:processCompletionConfirmed = $true
    if ($exitCode -ne 0) {
        $script:processCompletionReason = 'child_exit_nonzero'
        throw "$Label завершился с кодом $exitCode. Журналы: $testPath"
    }
    $script:processCompletionReason = 'complete'
}

function Assert-Marker {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or
        [IO.File]::ReadAllText($marker) -cne $markerValue) {
        throw "Пользовательские данные вне папки программы повреждены: $marker"
    }
}

function Assert-OwnedInstallation {
    $null = Assert-ArtifactPath $appPath
    $owner = Get-Content -LiteralPath $ownerFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($owner.run_id -ne $runId -or -not $owner.app_path.Equals($appPath, $comparison)) {
        throw 'Маркер владения QA не соответствует текущему запуску. Удаление запрещено.'
    }
    if (-not (Test-Path -LiteralPath $uninstallKey)) {
        throw 'Не найдена регистрация установленного QA экземпляра. Удаление запрещено.'
    }
    $registration = Get-ItemProperty -LiteralPath $uninstallKey
    $registeredPath = [IO.Path]::GetFullPath([string]$registration.InstallLocation).TrimEnd('\', '/')
    if (-not $registeredPath.Equals($appPath, $comparison)) {
        throw "Регистрация указывает на другую установку: $registeredPath. Изменение запрещено."
    }
    $null = Assert-ArtifactPath $uninstallerPath
    if (-not (Test-Path -LiteralPath $uninstallerPath -PathType Leaf)) {
        throw "Не найден собственный деинсталлятор QA: $uninstallerPath"
    }
    $registeredUninstaller = [string]$registration.UninstallString
    if ($registeredUninstaller -ne ('"' + $uninstallerPath + '"') -and
        $registeredUninstaller -ne $uninstallerPath) {
        throw 'Зарегистрирован другой деинсталлятор. Изменение запрещено.'
    }
}

function Invoke-OwnedUninstall {
    param([string]$LogName)
    Assert-OwnedInstallation
    $parameters=@{Program=$uninstallerPath; Arguments=@(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        ('/LOG=' + (Join-Path $testPath $LogName))); Label='Удаление QA'}
    if (Get-Variable -Name isElectron -ValueOnly -ErrorAction SilentlyContinue) { $parameters.TimeoutMilliseconds=600000 }
    Start-HiddenAndWait @parameters
}

function Invoke-InstalledSelfTest {
    param([string]$ReportName, [switch]$FromBundle)
    $electronMode = [bool](Get-Variable -Name isElectron -ValueOnly -ErrorAction SilentlyContinue)
    if (-not $FromBundle) { Assert-OwnedInstallation }
    $program = $appExecutable
    if ($electronMode) {
        $program = if ($FromBundle) { $referenceServiceExecutable } else { $serviceExecutable }
    } elseif ($FromBundle) { $program = $referenceExecutable }
    $reportPath = Join-Path $testPath $ReportName
    if (Test-Path -LiteralPath $reportPath) { throw "Отчёт QA уже существует: $reportPath" }
    $script:runtimeCleanupConfirmed = $false
    $parameters = @{Program=$program; Arguments=@('--self-test', $reportPath, '--data-dir', $dataPath);
        Label='Самопроверка установленного приложения'}
    if ($electronMode) { $parameters.TimeoutMilliseconds=600000 }
    Start-HiddenAndWait @parameters
    if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
        throw "Процесс завершился, но не создал JSON самопроверки: $reportPath"
    }
    if ($electronMode) {
        $item=Get-Item -LiteralPath $reportPath -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -gt 2097152) { throw 'INSTALLER_REPORT_UNSAFE' }
    }
    $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($report.ok -ne $true -or $report.model_ready -ne $true -or
        $report.scan_status -ne 'complete' -or $report.files -ne 3 -or
        $report.vector_count -ne 3 -or $report.dimension -ne 384 -or
        $report.literal_result -ne 'КМ-101 узлы фасада.dwg' -or
        $report.semantic_result -ne 'Контракт монтажных работ.docx' -or
        $report.backend -ne 'postgresql' -or $report.factory -ne 'open_search_service' -or
        $report.schema_version -ne 5 -or $report.clean_stop -ne $true -or
        $report.prior_schema_version -ne 5 -or $report.public_identity_preserved -ne $true -or
        $report.source_clean -ne $true -or $report.execution_mode -ne 'frozen' -or
        $report.source_commit -ne $sourceCommit -or
        $report.app_version -ne $buildManifest.app_version -or
        $report.core_version -ne $buildManifest.core_version -or
        $buildManifest.catalog.backend -ne 'postgresql' -or
        $report.schema_version -ne $buildManifest.catalog.schema_version -or
        $report.server_version -ne '18.6' -or $report.extension_version -ne '0.8.7' -or
        $report.catalog_id -le 0 -or
        $report.installation_id -notmatch '^[0-9a-f-]{36}$' -or
        $report.model_manifest_digest -notmatch '^[0-9a-f]{64}$' -or
        $report.index_fingerprint -notmatch '^[0-9a-f]{64}$' -or
        $report.model_key -notlike '*|filename-path-root-project-v2') {
        throw "Самопроверка не подтвердила автономную модель и поиск: $reportPath"
    }
    Assert-Marker
    $script:runtimeCleanupConfirmed = $true
    return $report
}

function Assert-DesktopProcessIdentity {
    param($Process, [int]$ExpectedId, [long]$ExpectedStartTicks, [string]$ExpectedExecutable)
    if ($Process.HasExited -or $Process.Id -ne $ExpectedId -or
        $Process.StartTime.Ticks -ne $ExpectedStartTicks -or
        -not $Process.Path.Equals($ExpectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'DESKTOP_PROCESS_IDENTITY_UNKNOWN'
    }
}

function Invoke-InstalledDesktopEvidence {
    if (-not $DesktopEvidenceDirectory) { return }
    Assert-OwnedInstallation
    $script:runtimeCleanupConfirmed = $false
    Start-HiddenAndWait -Program $appExecutable -Arguments @('--smoke-test', '--no-auto-scan',
        '--data-dir', $dataPath, '--screenshot', (Join-Path $testPath 'qt-offscreen.png')) -Label 'Offscreen Qt smoke'
    # The verified desktop exit contract includes terminal workers and successful
    # owned cleanup. A timeout, factory/close failure or unknown exit leaves false.
    $script:runtimeCleanupConfirmed = $true
    if ((Get-Process -Id $PID).SessionId -eq 0 -or -not [Environment]::UserInteractive -or
        $env:QT_QPA_PLATFORM -in @('offscreen','minimal')) { throw 'DESKTOP_INTERACTIVE_SESSION_REQUIRED' }
    $script:processCompletionConfirmed = $false
    $script:runtimeCleanupConfirmed = $false
    # Explicitly requested visible application; never launch a second shell.
    $gui = Start-Process -FilePath $appExecutable -ArgumentList @('--no-auto-scan','--data-dir',('"' + $dataPath + '"')) -PassThru
    $ownedId = $gui.Id; $ownedStart = $gui.StartTime.Ticks
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        $gui.Refresh()
        Assert-DesktopProcessIdentity $gui $ownedId $ownedStart $appExecutable
        if ($gui.MainWindowHandle -ne [IntPtr]::Zero) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($gui.MainWindowHandle -eq [IntPtr]::Zero) { throw 'DESKTOP_WINDOW_TIMEOUT_STATE_UNKNOWN' }
    @{ nonce=$DesktopEvidenceNonce; pid=$ownedId; start_ticks=$ownedStart; executable=$appExecutable;
       session_id=$gui.SessionId; window_handle=$gui.MainWindowHandle.ToInt64(); native_ui_verified=$false } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $DesktopEvidenceDirectory 'gui-ready.json') -Encoding UTF8
    $ackPath = Join-Path $DesktopEvidenceDirectory 'capture-ack.json'
    $deadline = [DateTime]::UtcNow.AddSeconds(180)
    $acknowledged = $false
    do {
        if (Test-Path -LiteralPath $ackPath -PathType Leaf) {
            # Copy may still be in progress; parse only a complete nonce-bound ack.
            try {
                $ack = Get-Content -LiteralPath $ackPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $acknowledged = $ack.nonce -ceq $DesktopEvidenceNonce -and $ack.captured -eq $true
            } catch { }
            if ($acknowledged) { break }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $acknowledged) { throw 'DESKTOP_CAPTURE_TIMEOUT_STATE_UNKNOWN' }
    $gui.Refresh()
    Assert-DesktopProcessIdentity $gui $ownedId $ownedStart $appExecutable
    if (-not $gui.CloseMainWindow() -or -not $gui.WaitForExit(30000)) { throw 'DESKTOP_CLOSE_TIMEOUT_STATE_UNKNOWN' }
    if ($gui.ExitCode -ne 0) { throw 'DESKTOP_EXIT_FAILED' }
    $script:processCompletionConfirmed = $true
    $script:runtimeCleanupConfirmed = $true
    @{ nonce=$DesktopEvidenceNonce; closed=$true; pid=$ownedId; start_ticks=$ownedStart } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $DesktopEvidenceDirectory 'gui-closed.json') -Encoding UTF8
}
function Invoke-PostgresProof {
    param([string]$ReportName, [string]$Mode, [switch]$FromBundle)
    $program = $appExecutable
    if ($FromBundle) { $program = $referenceExecutable } else { Assert-OwnedInstallation }
    $electronMode = [bool](Get-Variable -Name isElectron -ValueOnly -ErrorAction SilentlyContinue)
    if ($electronMode) {
        $program = if ($FromBundle) { $referenceServiceExecutable } else { $serviceExecutable }
    }
    $reportPath = Join-Path $testPath $ReportName
    if (Test-Path -LiteralPath $reportPath) { throw 'PostgreSQL QA report already exists.' }
    $script:runtimeCleanupConfirmed = $false
    $parameters = @{Program=$program; Arguments=@(
        '--postgres-self-test', $reportPath, '--data-dir', $postgresPath,
        '--postgres-qa-lifecycle', $Mode, '--postgres-qa-run', $runId); Label='PostgreSQL QA'}
    if ($electronMode) { $parameters.TimeoutMilliseconds=600000 }
    Start-HiddenAndWait @parameters
    if ($electronMode) {
        $item=Get-Item -LiteralPath $reportPath -Force
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            $item.Length -gt 2097152) { throw 'INSTALLER_REPORT_UNSAFE' }
    }
    $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $script:runtimeCleanupConfirmed = $report.clean_stop -eq $true
    if ($report.ok -ne $true -or $report.clean_stop -ne $true -or
        $report.profile_retained -ne $true -or $report.profile_removed -ne $false -or
        $report.source_commit -ne $sourceCommit -or $report.execution_mode -ne 'frozen' -or
        $report.runtime_api -ne 'PostgresRuntime' -or $report.settings_preserved -ne $true -or
        $report.preservation.source_verified -ne $true -or $report.dimension -ne 384) {
        throw "PostgreSQL lifecycle evidence failed: $reportPath"
    }
    if ($Mode -eq 'seed') {
        if ($report.backup.method -ne 'logical-schema' -or
            $report.restore.rows_equal -ne $true -or $report.restore.restart_verified -ne $true) {
            throw 'Installed PostgreSQL restore proof failed.'
        }
    } elseif ($report.preservation.source_installation_id -ne
              $summary.postgres_seed.preservation.source_installation_id -or
              $report.settings_sha256 -ne $summary.postgres_seed.settings_sha256 -or
              $report.persisted_rows_verified -ne $true) {
        throw 'Persisted installation identity, rows or settings changed.'
    }
    return $report
}

function Get-PreservedProfileSnapshot {
    param([string]$ProfilePath = $postgresPath)
    # Enumerate one directory at a time: reject junctions before descending.
    $null = Assert-ArtifactPath $ProfilePath -Qa
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($ProfilePath)
    $records = @()
    while ($pending.Count) {
        $directory = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Reparse point in preserved QA profile.'
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) } else {
                $records += [ordered]@{
                    path = $item.FullName.Substring($ProfilePath.Length + 1)
                    bytes = $item.Length
                    sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
                }
            }
        }
    }
    return ($records | Sort-Object { $_.path } | ConvertTo-Json -Depth 4 -Compress)
}

function Assert-ShortcutTargets {
    $shell = New-Object -ComObject WScript.Shell
    try {
        $helpRelative = if (Get-Variable -Name isElectron -ValueOnly -ErrorAction SilentlyContinue) {
            'service\_internal\resources\help.html'
        } else { '_internal\resources\help.html' }
        foreach ($entry in @(
            @{name='ssearch.lnk'; target=$appExecutable},
            @{name='Справка ssearch.lnk'; target=(Join-Path $appPath $helpRelative)},
            @{name='Удалить ssearch.lnk'; target=$uninstallerPath})) {
            $link = Join-Path $groupPath $entry.name
            if (-not (Test-Path -LiteralPath $link -PathType Leaf)) { throw 'QA shortcut missing.' }
            $shortcut = $shell.CreateShortcut($link)
            if (-not ([IO.Path]::GetFullPath($shortcut.TargetPath)).Equals($entry.target, $comparison)) {
                throw 'QA shortcut points outside its owned installation.'
            }
        }
    } finally {
        $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    }
}

function Invoke-InstallerHelper {
    param([string[]]$Arguments)
    $value = & $Python (Join-Path $repository 'tools/ci_installer.py') @Arguments
    foreach ($line in @($value)) {
        if ($line -cmatch '^CI_INSTALLER_INVENTORY files=[0-9]{1,6} desktop_match=(true|false) service_match=(true|false) inventory_match=(true|false|unavailable)$' -or
            $line -cmatch '^CI_INSTALLER_HELPER code=(installer_inventory_boundary|installer_inno_inventory_invalid|installer_non_file|installer_duplicate_file|installer_inno_identity_changed|installer_inno_file_missing|installer_payload_image_mismatch|installer_inventory_mismatch|installer_reparse_refused|installer_report_unavailable|installer_object_required|unavailable)$') {
            Write-Host $line
        }
    }
    if ($LASTEXITCODE -ne 0) { throw 'INSTALLER_CONTRACT_FAILED' }
    return $value
}

function Get-OwnedInnoFiles {
    $records = @{}
    foreach ($name in @('unins000.exe', 'unins000.dat')) {
        $path = Join-Path $appPath $name
        $null = Assert-ArtifactPath $path
        $item = Get-Item -LiteralPath $path -Force
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'INSTALLER_INNO_FILE_UNSAFE'
        }
        $records[$name] = @{size=$item.Length; sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()}
    }
    return $records
}

function Assert-ElectronOwnership {
    Assert-OwnedInstallation
    Assert-ShortcutTargets
    $actualFiles=Get-OwnedInnoFiles
    foreach ($name in @('unins000.exe', 'unins000.dat')) {
        $actual=$actualFiles[$name]
        if ($actual.size -ne $ownedInno[$name].size -or $actual.sha256 -cne $ownedInno[$name].sha256) {
            throw 'INSTALLER_INNO_OWNERSHIP_CHANGED'
        }
    }
    foreach ($record in $shortcutRecords) {
        $item = Get-Item -LiteralPath $record.path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            (Get-FileHash -LiteralPath $record.path -Algorithm SHA256).Hash -cne $record.sha256) {
            throw 'INSTALLER_SHORTCUT_OWNERSHIP_CHANGED'
        }
    }
}

function Assert-ElectronInventory {
    param([string]$Label, [switch]$Reference)
    $directory = if ($Reference) { $referencePath } else { $appPath }
    $ownedPath = Join-Path $testPath "$Label-owned.json"
    $value = if ($Reference) { @{} } else { $ownedInno }
    [IO.File]::WriteAllText($ownedPath, ($value | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $null = Invoke-InstallerHelper @('inventory', '--directory', $directory, '--manifest', $manifestPath,
        '--owned', $ownedPath, '--output', (Join-Path $testPath "$Label-inventory.json"))
}

function Invoke-InstalledUiProof {
    param([string]$Scenario, [string]$Label)
    Assert-ElectronOwnership
    $script:runtimeCleanupConfirmed=$false
    $report = Join-Path $testPath "$Label-ui-$Scenario.json"
    $profile = Join-Path $testPath "$Label UI $Scenario Кириллица with spaces"
    $budget = [int]$uiPolicy.budgets_ms.$Scenario + 120000
    Start-HiddenAndWait -Program $appExecutable -Arguments @('--ui-self-test', $report,
        '--ui-scenario', $Scenario, '--data-dir', $profile) -Label 'Installed hidden UI proof' -TimeoutMilliseconds $budget
    $null = Invoke-InstallerHelper @('ui', '--directory', $appPath, '--manifest', $manifestPath,
        '--report', $report, '--scenario', $Scenario)
    $script:runtimeCleanupConfirmed=$true
}

if ($Electron) {
    # This branch consumes the original Quick artifact, never a source build.
    # The enclosing CI wrapper already verified the loaded standard-user profile,
    # DPAPI and owned retained job. Keep its actual KnownFolders/environment.
    $isElectron=$true
    if ($env:GITHUB_ACTIONS -cne 'true' -or $env:BUILD_MODE -cne 'installer' -or
        -not $Python -or $DesktopEvidenceDirectory -or $DesktopEvidenceNonce -or
        -not [Environment]::Is64BitProcess) { throw 'INSTALLER_CI_CONTEXT_REQUIRED' }
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'INSTALLER_STANDARD_USER_REQUIRED' }
    $pins = Get-Content -LiteralPath (Join-Path $repository 'resources/ci/installer-proof-input.json') -Raw | ConvertFrom-Json
    $null=Invoke-InstallerHelper @('admit')
    $sourceCommit=$pins.source_commit
    $buildRoot=Join-Path $artifacts ('development-quick\' + $sourceCommit)
    $installerPath=Assert-ArtifactPath $Installer
    $testPath=Assert-ArtifactPath $TestDir -Qa
    $manifestPath=Join-Path $buildRoot 'build-manifest.json'
    $buildManifest=Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $installerPath.Equals((Join-Path $buildRoot ('artifacts\' + $pins.installer_name)), $comparison) -or
        -not $testPath.StartsWith((Join-Path $buildRoot 'qa') + '\', $comparison) -or
        $buildManifest.source_commit -cne $sourceCommit -or $buildManifest.payload_kind -cne 'electron-service' -or
        (Get-Item -LiteralPath $installerPath).Length -ne $pins.installer_size -or
        (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash -ine $pins.installer_sha256 -or
        (Test-Path -LiteralPath $testPath)) { throw 'INSTALLER_INPUT_BOUNDARY' }
    foreach ($key in @($uninstallKey,
        'HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1')) {
        if (Test-Path -LiteralPath $key) { throw 'INSTALLER_FOREIGN_REGISTRATION' }
    }
    if (Get-Process -Name 'ssearch','ssearch-service','AtomSearch','KaizenSearch' -ErrorAction SilentlyContinue) {
        throw 'INSTALLER_FOREIGN_PROCESS'
    }
    $programsPath=[Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
    if (-not $programsPath) { throw 'INSTALLER_PROGRAMS_UNAVAILABLE' }
    $groupPath=Join-Path $programsPath $groupName
    if (Test-Path -LiteralPath $groupPath) { throw 'INSTALLER_FOREIGN_SHORTCUTS' }
    $null=Invoke-InstallerHelper @('private', '--directory', $testPath)
    $appPath=Join-Path $testPath 'Installed приложение with spaces'
    $appExecutable=Join-Path $appPath 'ssearch.exe'
    $serviceExecutable=Join-Path $appPath 'service/ssearch-service.exe'
    $referencePath=Join-Path $testPath 'Reference поставка with spaces'
    $referenceExecutable=Join-Path $referencePath 'ssearch.exe'
    $referenceServiceExecutable=Join-Path $referencePath 'service/ssearch-service.exe'
    $uninstallerPath=Join-Path $appPath 'unins000.exe'
    $dataPath=Join-Path $testPath 'Actual application данные with spaces'
    $postgresPath=Join-Path $testPath 'Postgres данные with spaces'
    $ownerFile=Join-Path $testPath 'qa-owner.json'
    $marker=Join-Path $dataPath 'preserve-marker.txt'
    $markerValue="ssearch preserved QA $runId"
    $summaryPath=Join-Path $testPath 'installer-qa.json'
    $runtimeCleanupConfirmed=$true; $processCompletionConfirmed=$true
    $null=New-Item -ItemType Directory -Path $dataPath
    [IO.File]::WriteAllText($marker,$markerValue)
    @{run_id=$runId;app_path=$appPath} | ConvertTo-Json | Set-Content -LiteralPath $ownerFile -Encoding UTF8
    $os=Get-CimInstance Win32_OperatingSystem
    $checks=[ordered]@{}
    foreach ($name in @('fresh_install_verified','same_version_reinstall_verified','owned_uninstall_verified',
        'payload_inventory_verified','shortcut_ownership_verified','application_bytes_preserved',
        'application_index_preserved','postgres_bytes_preserved','postgres_restart_verified',
        'installed_proofs_passed','runtime_cleanup_confirmed','process_completion_confirmed',
        'registered_installation_removed','owned_shortcuts_removed','standard_user_profile_verified')) { $checks[$name]=$false }
    $checks.standard_user_profile_verified=$true
    $summary=[ordered]@{ok=$false; source_commit=$sourceCommit; installer_sha256=$pins.installer_sha256;
        installer_bytes=$pins.installer_size; inventory_sha256=$buildManifest.desktop.inventory_sha256;
        os_product_type=[int]$os.ProductType; os_build=[int]$os.BuildNumber; checks=$checks;
        postgres_seed=$null; error_code=$null}
    $ownedInno=$null; $shortcutRecords=@(); $uninstalled=$false; $failed=$false
    $phase='fresh_install'
    $freshStep='policy'
    Write-Host "CI_INSTALLER_PHASE $phase started"
    try {
        Write-Host "CI_INSTALLER_FRESH $freshStep started"
        $uiPolicy=(Invoke-InstallerHelper @('ui-policy')) | ConvertFrom-Json
        Write-Host "CI_INSTALLER_FRESH $freshStep pass"
        $installArguments=@('/VERYSILENT','/SUPPRESSMSGBOXES','/CURRENTUSER','/NORESTART','/SP-',
            '/NOCLOSEAPPLICATIONS','/NORESTARTAPPLICATIONS','/MERGETASKS=!desktopicon',
            ('/DIR=' + $appPath),('/GROUP=' + $groupName))
        $freshStep='install_process'
        Write-Host "CI_INSTALLER_FRESH $freshStep started"
        Start-HiddenAndWait $installerPath ($installArguments + @('/LOG=' + (Join-Path $testPath 'fresh.log'))) 'Fresh install' 600000
        Write-Host "CI_INSTALLER_FRESH $freshStep pass"
        $freshStep='registration'
        Write-Host "CI_INSTALLER_FRESH $freshStep started"
        Assert-OwnedInstallation
        Write-Host "CI_INSTALLER_FRESH $freshStep pass"
        $freshStep='inno_records'
        Write-Host "CI_INSTALLER_FRESH $freshStep started"
        $ownedInno=Get-OwnedInnoFiles
        Write-Host "CI_INSTALLER_FRESH $freshStep pass"
        $freshStep='inventory'
        Write-Host "CI_INSTALLER_FRESH $freshStep started"
        Assert-ElectronInventory 'fresh'
        Write-Host "CI_INSTALLER_FRESH $freshStep pass"
        $freshStep='shortcuts'
        Write-Host "CI_INSTALLER_FRESH $freshStep started"
        Assert-ShortcutTargets
        $links=@(Get-ChildItem -LiteralPath $groupPath -Force)
        if ($links.Count -ne 3) { throw 'INSTALLER_SHORTCUT_INVENTORY' }
        $shortcutRecords=@(foreach ($link in $links) {
            if ($link.PSIsContainer -or ($link.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'INSTALLER_SHORTCUT_UNSAFE' }
            @{path=$link.FullName;sha256=(Get-FileHash -LiteralPath $link.FullName -Algorithm SHA256).Hash}
        })
        $checks.fresh_install_verified=$true
        Write-Host "CI_INSTALLER_FRESH $freshStep pass"
        $freshStep='reference_copy'
        Write-Host "CI_INSTALLER_FRESH $freshStep started"
        # Copy only the verified first installed closure; no dist/source fallback.
        $inventory=Get-Content -LiteralPath (Join-Path $testPath 'fresh-inventory.json') -Raw | ConvertFrom-Json
        $null=New-Item -ItemType Directory -Path $referencePath
        foreach ($record in $inventory.files.PSObject.Properties) {
            $target=Join-Path $referencePath $record.Name
            $null=New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force
            Copy-Item -LiteralPath (Join-Path $appPath $record.Name) -Destination $target
        }
        Write-Host "CI_INSTALLER_FRESH $freshStep pass"
        $freshStep='reference_inventory'
        Write-Host "CI_INSTALLER_FRESH $freshStep started"
        Assert-ElectronInventory 'reference-first' -Reference
        Write-Host "CI_INSTALLER_FRESH $freshStep pass"
        Write-Host "CI_INSTALLER_PHASE $phase pass"
        foreach ($scenario in @('security','load','lifecycle','empty-profile')) {
            $phase='ui_' + $scenario.Replace('-','_')
            Write-Host "CI_INSTALLER_PHASE $phase started"
            Invoke-InstalledUiProof $scenario 'fresh'
            Write-Host "CI_INSTALLER_PHASE $phase pass"
        }
        $phase='ordinary_serve'
        Write-Host "CI_INSTALLER_PHASE $phase started"
        $runtimeCleanupConfirmed=$false
        $serveReport=Join-Path $testPath 'ordinary-serve.json'
        Start-HiddenAndWait $Python @((Join-Path $repository 'tools/check_frozen_service.py'),
            '--service-executable',$serviceExecutable,'--qa-directory',(Join-Path $testPath 'Serve profiles Кириллица'),
            '--source-commit',$sourceCommit,'--app-version',$buildManifest.app_version,'--report',$serveReport) 'Installed ordinary serve' 600000
        $null=Invoke-InstallerHelper @('serve','--report',$serveReport,'--manifest',$manifestPath)
        Write-Host "CI_INSTALLER_PHASE $phase pass"
        $phase='service_owner'
        Write-Host "CI_INSTALLER_PHASE $phase started"
        $ownerReport=Join-Path $testPath 'service-owner.json'
        Start-HiddenAndWait $serviceExecutable @('--service-test',$ownerReport,'--data-dir',
            (Join-Path $testPath 'Owner profile Кириллица')) 'Installed owner proof' 600000
        $null=Invoke-InstallerHelper @('owner','--report',$ownerReport,'--manifest',$manifestPath)
        $runtimeCleanupConfirmed=$true
        Write-Host "CI_INSTALLER_PHASE $phase pass"
        $phase='application'
        Write-Host "CI_INSTALLER_PHASE $phase started"
        $first=Invoke-InstalledSelfTest 'application-first.json'
        $null=Invoke-InstallerHelper @('application','--report',(Join-Path $testPath 'application-first.json'),'--manifest',$manifestPath)
        Write-Host "CI_INSTALLER_PHASE $phase pass"
        $phase='pg_seed'
        Write-Host "CI_INSTALLER_PHASE $phase started"
        $summary.postgres_seed=Invoke-PostgresProof 'postgres-seed.json' 'seed'
        Write-Host "CI_INSTALLER_PHASE $phase pass"
        $phase='reinstall'
        Write-Host "CI_INSTALLER_PHASE $phase started"
        Assert-ElectronOwnership
        Assert-ElectronInventory 'before-reinstall'
        $pgBefore=Get-PreservedProfileSnapshot
        $appBefore=Get-PreservedProfileSnapshot -ProfilePath $dataPath
        Start-HiddenAndWait $installerPath ($installArguments + @('/LOG=' + (Join-Path $testPath 'reinstall.log'))) 'Same version reinstall' 600000
        # Inno's own .dat may be appended by a successful reinstall. Its new
        # identity is admitted only after the retained installer and registration.
        Assert-OwnedInstallation
        $ownedInno=Get-OwnedInnoFiles
        Assert-ElectronOwnership
        Assert-ElectronInventory 'reinstalled'
        if ((Get-PreservedProfileSnapshot) -cne $pgBefore -or
            (Get-PreservedProfileSnapshot -ProfilePath $dataPath) -cne $appBefore) { throw 'INSTALLER_REINSTALL_CHANGED_PROFILE' }
        $repeat=Invoke-InstalledSelfTest 'application-repeat.json'
        $null=Invoke-InstallerHelper @('application','--report',(Join-Path $testPath 'application-repeat.json'),'--manifest',$manifestPath)
        $null=Invoke-InstallerHelper @('preservation','--first',(Join-Path $testPath 'application-first.json'),
            '--report',(Join-Path $testPath 'application-repeat.json'))
        $null=Invoke-PostgresProof 'postgres-repeat.json' 'verify'
        Invoke-InstalledUiProof 'load' 'reinstalled'
        $checks.same_version_reinstall_verified=$true
        Write-Host "CI_INSTALLER_PHASE $phase pass"
        $phase='uninstall'
        Write-Host "CI_INSTALLER_PHASE $phase started"
        Assert-ElectronOwnership
        Assert-ElectronInventory 'before-uninstall'
        Assert-ElectronInventory 'reference-before-uninstall' -Reference
        $pgBefore=Get-PreservedProfileSnapshot
        $appBefore=Get-PreservedProfileSnapshot -ProfilePath $dataPath
        if (-not $runtimeCleanupConfirmed -or -not $processCompletionConfirmed) { throw 'INSTALLER_RUNTIME_CLEANUP_UNKNOWN' }
        Invoke-OwnedUninstall 'uninstall.log'
        if ((Test-Path -LiteralPath $uninstallKey) -or (Test-Path -LiteralPath $appExecutable) -or
            (Test-Path -LiteralPath $groupPath)) { throw 'INSTALLER_OWNED_REMOVAL_INCOMPLETE' }
        $uninstalled=$true
        Assert-Marker
        if ((Get-PreservedProfileSnapshot) -cne $pgBefore -or
            (Get-PreservedProfileSnapshot -ProfilePath $dataPath) -cne $appBefore) { throw 'INSTALLER_UNINSTALL_CHANGED_PROFILE' }
        Write-Host "CI_INSTALLER_PHASE $phase pass"
        $phase='post_uninstall_app'
        Write-Host "CI_INSTALLER_PHASE $phase started"
        $null=Invoke-InstalledSelfTest 'application-after-uninstall.json' -FromBundle
        $null=Invoke-InstallerHelper @('application','--report',(Join-Path $testPath 'application-after-uninstall.json'),'--manifest',$manifestPath)
        foreach ($name in @('application-first.json','application-repeat.json')) {
            $null=Invoke-InstallerHelper @('preservation','--first',(Join-Path $testPath $name),
                '--report',(Join-Path $testPath 'application-after-uninstall.json'))
        }
        Write-Host "CI_INSTALLER_PHASE $phase pass"
        $phase='post_uninstall_pg'
        Write-Host "CI_INSTALLER_PHASE $phase started"
        $null=Invoke-PostgresProof 'postgres-after-uninstall.json' 'verify' -FromBundle
        Assert-ElectronInventory 'reference-final' -Reference
        Write-Host "CI_INSTALLER_PHASE $phase pass"
        foreach ($name in @('owned_uninstall_verified','payload_inventory_verified','shortcut_ownership_verified',
            'application_bytes_preserved','application_index_preserved','postgres_bytes_preserved',
            'postgres_restart_verified','installed_proofs_passed','registered_installation_removed','owned_shortcuts_removed')) { $checks[$name]=$true }
        $summary.ok=$true
    } catch {
        if ($phase -ceq 'fresh_install') {
            # Capture the failed operation before cleanup can replace the last
            # retained waiter's completion observation with a successful one.
            $hresult=[int]$_.Exception.HResult
            $waitReason=Get-Variable -Name processCompletionReason -Scope Script -ValueOnly -ErrorAction SilentlyContinue
            if ($waitReason -cnotin @('preparing_waiter','waiting','capture_limit','waiter_timeout',
                'reading_waiter_exit','checking_completion_file','waiter_exit_unavailable','waiter_exit_nonzero',
                'completion_missing','reading_completion','completion_payload_invalid','child_exit_nonzero','complete')) { $waitReason='unavailable' }
            $payload=Get-Variable -Name processCompletionPayload -Scope Script -ValueOnly -ErrorAction SilentlyContinue
            $integer=($null -ne $payload -and $payload.exit_integer -eq $true).ToString().ToLowerInvariant()
            $zero=($null -ne $payload -and $payload.exit_zero -eq $true).ToString().ToLowerInvariant()
            Write-Host "CI_INSTALLER_FRESH $freshStep fail"
            Write-Host "CI_INSTALLER_FAILURE substep=$freshStep hresult=$hresult wait_reason=$waitReason exit_integer=$integer exit_zero=$zero"
        }
        Write-Host "CI_INSTALLER_PHASE $phase fail"
        $failed=$true
        $summary.error_code='INSTALLER_LIFECYCLE_FAILED'
    } finally {
        if (-not $uninstalled -and $null -ne $ownedInno -and $runtimeCleanupConfirmed -and $processCompletionConfirmed) {
            try {
                Assert-ElectronOwnership
                Invoke-OwnedUninstall 'uninstall-after-failure.log'
            } catch { $summary.error_code='INSTALLER_CLEANUP_UNCONFIRMED' }
        }
        $checks.runtime_cleanup_confirmed=[bool]$runtimeCleanupConfirmed
        $checks.process_completion_confirmed=[bool]$processCompletionConfirmed
        [IO.File]::WriteAllText($summaryPath,($summary | ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
    }
    if ($failed) { exit 1 }
    exit 0
}

# All checks before the first write or process launch are read-only. In
# particular, a real existing install is never upgraded or uninstalled by QA.
if (-not [Environment]::Is64BitProcess) { throw 'Запустите сценарий через PowerShell x64.' }
if ($DesktopEvidenceDirectory) {
    $expectedEvidence = Join-Path (Split-Path $repository -Parent) 'evidence'
    if ($DesktopEvidenceNonce -notmatch '^[a-f0-9]{32}$' -or
        -not [IO.Path]::GetFullPath($DesktopEvidenceDirectory).Equals($expectedEvidence, $comparison) -or
        -not (Test-Path -LiteralPath $DesktopEvidenceDirectory -PathType Container)) { throw 'DESKTOP_EVIDENCE_BOUNDARY' }
    $cursor = $DesktopEvidenceDirectory
    while ($cursor) {
        if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'DESKTOP_EVIDENCE_REPARSE' }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
    foreach ($name in @('gui-ready.json','gui-closed.json','capture-ack.json')) {
        if (Test-Path -LiteralPath (Join-Path $DesktopEvidenceDirectory $name)) { throw 'DESKTOP_EVIDENCE_EXISTS' }
    }
} elseif ($DesktopEvidenceNonce) { throw 'DESKTOP_EVIDENCE_DIRECTORY_REQUIRED' }
$installerPath = Assert-ArtifactPath $Installer
$testPath = Assert-ArtifactPath $TestDir -Qa
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf) -or
    [IO.Path]::GetExtension($installerPath) -ne '.exe') {
    throw "Не найден EXE установщика: $installerPath"
}
$relativeInstaller = $installerPath.Substring($artifacts.Length + 1)
$sourceCommit = $relativeInstaller.Split('\')[1]
$buildMode = $relativeInstaller.Split('\')[0]
$buildRoot = Join-Path $artifacts ($buildMode + '\' + $sourceCommit)
if (-not $testPath.StartsWith((Join-Path $buildRoot 'qa') + '\', $comparison)) {
    throw 'Installer and QA directory must belong to the same build mode and source commit.'
}
$buildManifest = Get-Content -LiteralPath (Join-Path $buildRoot 'build-manifest.json') -Raw -Encoding UTF8 |
    ConvertFrom-Json
$expectedInstaller = Assert-ArtifactPath (Join-Path $buildRoot $buildManifest.installer.path)
if ($buildManifest.source_commit -ne $sourceCommit -or
    -not $installerPath.Equals($expectedInstaller, $comparison) -or
    (Get-Item -LiteralPath $installerPath).Length -ne $buildManifest.installer.size -or
    (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash -ne $buildManifest.installer.sha256) {
    throw 'Installer does not match the verified clean-build manifest.'
}
$referenceExecutable = Assert-ArtifactPath (Join-Path $buildRoot 'dist\ssearch\ssearch.exe')
if (-not (Test-Path -LiteralPath $referenceExecutable -PathType Leaf)) {
    throw 'Verified bundle executable required for post-uninstall persisted data proof.'
}
if ($buildManifest.executable.path -ne 'dist/ssearch/ssearch.exe' -or
    (Get-Item -LiteralPath $referenceExecutable).Length -ne $buildManifest.executable.size -or
    (Get-FileHash -LiteralPath $referenceExecutable -Algorithm SHA256).Hash -ne
        $buildManifest.executable.sha256) {
    throw 'Portable executable differs from the clean-build identity evidence.'
}
$frozenProof = Get-Content -LiteralPath (Join-Path $buildRoot $buildManifest.qa.postgresql) -Raw -Encoding UTF8 |
    ConvertFrom-Json
if ($frozenProof.ok -ne $true -or $frozenProof.source_commit -ne $sourceCommit -or
    $frozenProof.execution_mode -ne 'frozen') {
    throw 'Embedded executable source identity is unconfirmed.'
}
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run QA as an ordinary user, without elevation.'
}
$osInformation = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$osFamily = if ([int]$osInformation.CurrentBuild -ge 22000) { 'Windows 11' } else { 'Windows 10' }
foreach ($key in @($uninstallKey,
    'HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1',
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{1EAB882B-9360-4FC5-9538-E86CB53AA2A1}_is1')) {
    if (Test-Path -LiteralPath $key) {
        throw "ssearch, Atom Search или Kaizen Search уже установлен ($key). QA остановлена без изменений."
    }
}
if (Get-Process -Name 'ssearch', 'AtomSearch', 'KaizenSearch' -ErrorAction SilentlyContinue) {
    throw 'Работает ssearch.exe, AtomSearch.exe или KaizenSearch.exe. Завершите приложение перед изолированной QA.'
}
if (Test-Path -LiteralPath $testPath) {
    throw "Нужен новый, ещё не существующий каталог QA внутри artifacts: $testPath"
}
$programsPath = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
if (-not $programsPath) { throw 'Не удалось определить пользовательское меню «Пуск».' }
$groupPath = Join-Path $programsPath $groupName
if (Test-Path -LiteralPath $groupPath) { throw "Группа QA уже существует: $groupPath" }
$appPath = Assert-ArtifactPath (Join-Path $testPath 'app')
$appExecutable = Join-Path $appPath 'ssearch.exe'
$uninstallerPath = Join-Path $appPath 'unins000.exe'
$dataPath = Join-Path $testPath 'user-data'
$postgresPath = Join-Path $testPath 'postgres-profile'
$marker = Join-Path $dataPath 'preserve-marker.txt'
$markerValue = "ssearch QA preserved data $runId"
$ownerFile = Join-Path $testPath 'qa-owner.json'
$summaryPath = Join-Path $testPath 'installer-qa.json'
$runtimeCleanupConfirmed = $true
$processCompletionConfirmed = $true
$qaEnvironment = @{}
foreach ($name in @('LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP')) {
    $qaEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

New-Item -ItemType Directory -Path $dataPath | Out-Null
[IO.File]::WriteAllText($marker, $markerValue, [Text.Encoding]::UTF8)
@{ run_id = $runId; test_path = $testPath; app_path = $appPath; group = $groupName } |
    ConvertTo-Json | Set-Content -LiteralPath $ownerFile -Encoding UTF8
$summary = [ordered]@{ ok = $false; installer = $installerPath; run_id = $runId;
    source_commit = $sourceCommit; qualification = 'current Windows development host';
    clean_os = $false; win10_verified = $false; network_disabled = $false;
    ordinary_user = $true;
    os_version = [Environment]::OSVersion.Version.ToString();
    os_family = $osFamily; os_build = "$($osInformation.CurrentBuild).$($osInformation.UBR)";
    os_display_version = $osInformation.DisplayVersion;
    installer_sha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash;
    installer_bytes = (Get-Item -LiteralPath $installerPath).Length;
    app_path = $appPath; data_path = $dataPath; group = $groupName;
    postgres_path = $postgresPath; postgres_seed = $null; postgres_after_reinstall = $null;
    postgres_after_uninstall = $null; postgres_bytes_preserved_reinstall = $false;
    postgres_bytes_preserved_uninstall = $false;
    shortcut_targets_verified = $false; marker_preserved = $false;
    first_self_test = $null; repeated_self_test = $null; data_preserved = $false;
    uninstalled = $false; shortcuts_removed = $false; error = $null; cleanup_error = $null }
$failure = $null
try {
    foreach ($name in @('LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP')) {
        $folder = Join-Path $testPath $name.ToLowerInvariant()
        New-Item -ItemType Directory -Path $folder | Out-Null
        [Environment]::SetEnvironmentVariable($name, $folder, 'Process')
    }
    $installArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/CURRENTUSER', '/NORESTART',
        '/SP-', '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS', '/MERGETASKS=!desktopicon',
        ('/DIR=' + $appPath), ('/GROUP=' + $groupName))
    Start-HiddenAndWait -Program $installerPath -Arguments (
        $installArguments + @('/LOG=' + (Join-Path $testPath 'install-first.log'))) -Label 'Первая установка QA'
    Assert-OwnedInstallation
    if (-not (Test-Path -LiteralPath $groupPath -PathType Container) -or
        @(Get-ChildItem -LiteralPath $groupPath -Filter '*.lnk' -File).Count -lt 3) {
        throw "Установщик не создал три ярлыка в изолированной группе: $groupPath"
    }
    Assert-ShortcutTargets
    $summary.shortcut_targets_verified = $true
    Assert-Marker
    $summary.first_self_test = Invoke-InstalledSelfTest 'self-test-first.json'
    if ((Get-FileHash -LiteralPath $appExecutable -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $referenceExecutable -Algorithm SHA256).Hash) {
        throw 'Installed executable differs from the clean build.'
    }
    $summary.postgres_seed = Invoke-PostgresProof 'postgres-seed.json' 'seed'
    $beforeReinstall = Get-PreservedProfileSnapshot
    $applicationBeforeReinstall = Get-PreservedProfileSnapshot -ProfilePath $dataPath

    Assert-OwnedInstallation
    Start-HiddenAndWait -Program $installerPath -Arguments (
        $installArguments + @('/LOG=' + (Join-Path $testPath 'install-repeat.log'))) -Label 'Повторная установка QA'
    Assert-Marker
    Assert-ShortcutTargets
    if ((Get-PreservedProfileSnapshot) -cne $beforeReinstall) {
        throw 'Reinstall changed retained PGDATA, credentials or settings.'
    }
    $summary.postgres_bytes_preserved_reinstall = $true
    if ((Get-PreservedProfileSnapshot -ProfilePath $dataPath) -cne $applicationBeforeReinstall) {
        throw 'Reinstall changed the actual application profile.'
    }
    $summary.repeated_self_test = Invoke-InstalledSelfTest 'self-test-repeat.json'
    if ($summary.repeated_self_test.prior_index_verified -ne $true -or
        $summary.repeated_self_test.prior_files -ne 3 -or
        $summary.repeated_self_test.installation_id -ne $summary.first_self_test.installation_id -or
        $summary.repeated_self_test.catalog_id -ne $summary.first_self_test.catalog_id -or
        $summary.repeated_self_test.model_manifest_digest -ne $summary.first_self_test.model_manifest_digest -or
        $summary.repeated_self_test.prior_index_fingerprint -ne $summary.first_self_test.index_fingerprint) {
        throw 'postgres_application_index_not_preserved'
    }
    $summary.application_index_preserved_reinstall = $true
    $summary.postgres_after_reinstall = Invoke-PostgresProof 'postgres-after-reinstall.json' 'verify'
    Invoke-InstalledDesktopEvidence
    $beforeUninstall = Get-PreservedProfileSnapshot
    $applicationBeforeUninstall = Get-PreservedProfileSnapshot -ProfilePath $dataPath
    if (-not $runtimeCleanupConfirmed -or -not $processCompletionConfirmed) {
        throw 'Application cleanup unconfirmed; no uninstall attempted.'
    }
    Invoke-OwnedUninstall 'uninstall.log'
    Assert-Marker
    if (Test-Path -LiteralPath $uninstallKey) { throw 'После удаления осталась регистрация QA.' }
    if (Test-Path -LiteralPath $appExecutable) { throw 'После удаления остался исполняемый файл QA.' }
    if (Test-Path -LiteralPath $groupPath) { throw 'После удаления осталась группа ярлыков QA.' }
    if ((Get-PreservedProfileSnapshot) -cne $beforeUninstall) {
        throw 'Uninstall changed retained PGDATA, credentials or settings.'
    }
    $summary.postgres_bytes_preserved_uninstall = $true
    if ((Get-PreservedProfileSnapshot -ProfilePath $dataPath) -cne $applicationBeforeUninstall) {
        throw 'Uninstall changed the actual application profile.'
    }
    $summary.uninstalled = $true
    $summary.postgres_after_uninstall = Invoke-PostgresProof 'postgres-after-uninstall.json' 'verify' -FromBundle
    $summary.data_preserved = $true
    $summary.uninstalled = $true
    $summary.shortcuts_removed = $true
    $summary.ok = $true
} catch {
    $failure = $_.Exception.Message
    $summary.error = $failure
} finally {
    # On failure, clean up only when both the owner marker and the registered
    # installation still prove this is our own bounded QA instance.
    if (-not $summary.uninstalled -and $runtimeCleanupConfirmed -and $processCompletionConfirmed -and
        (Test-Path -LiteralPath $uninstallKey)) {
        try {
            Invoke-OwnedUninstall 'uninstall-after-failure.log'
            Assert-Marker
            $summary.uninstalled = -not (Test-Path -LiteralPath $uninstallKey)
            $summary.marker_preserved = $true
            $summary.shortcuts_removed = -not (Test-Path -LiteralPath $groupPath)
        } catch {
            $summary.cleanup_error = $_.Exception.Message
            Write-Warning "Автоматическая очистка QA не выполнена: $($summary.cleanup_error)"
        }
    }
    if (-not $runtimeCleanupConfirmed) {
        $summary.cleanup_error = 'Owned PostgreSQL shutdown unconfirmed; installation and data preserved.'
    }
    if (-not $processCompletionConfirmed) {
        $summary.cleanup_error = 'Child completion unconfirmed; no uninstall attempted.'
    }
    try {
        $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
    } finally {
        foreach ($name in @('LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP')) {
            [Environment]::SetEnvironmentVariable($name, $qaEnvironment[$name], 'Process')
        }
    }
}
if ($failure) { throw "$failure Отчёт: $summaryPath" }
Write-Output "QA пройдена. Отчёт: $summaryPath"
Write-Output "Данные сохранены: $marker"
