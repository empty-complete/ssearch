[CmdletBinding()]
param(
    [ValidateSet('Preflight','Prepare','Run')][string]$Mode = 'Preflight',
    [string]$Vmrun = 'C:\Program Files\VMware\VMware Workstation\vmrun.exe',
    [string]$Vmx, [string]$BuildDirectory, [string]$OutputDirectory,
    [string]$CheckerRevision, [string]$GuestRoot, [PSCredential]$Credential,
    [ValidateRange(10,18000)][int]$RunTimeoutSeconds = 7200
)
. (Join-Path $PSScriptRoot 'check_windows_guest.ps1')

function ConvertTo-QAArgument {
    param([AllowEmptyString()][string]$Value)
    '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

function Invoke-QAProcess {
    param([string]$Program, [string[]]$Arguments, [ValidateRange(1,1800000)][int]$TimeoutMilliseconds = 60000)
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $Program
    $start.Arguments = (($Arguments | ForEach-Object { ConvertTo-QAArgument $_ }) -join ' ')
    $start.UseShellExecute = $false; $start.CreateNoWindow = $true
    $start.RedirectStandardError = $true; $start.RedirectStandardOutput = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        try { $null = $process.Start() } catch { throw 'PROCESS_START_FAILED' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) { throw 'PROCESS_TIMEOUT_STATE_UNKNOWN' }
        if (-not [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($stdout,$stderr), 1000)) { throw 'PROCESS_PIPE_TIMEOUT_STATE_UNKNOWN' }
        if ($process.ExitCode -ne 0) { throw 'PROCESS_EXIT' }
        return $stdout.Result
    } finally {
        $start.Arguments = ''; $Arguments = $null
        # Disposing handles never kills vmrun or guest children.
        $process.Dispose()
    }
}

function Invoke-VmCommand {
    param([string]$Command, [string[]]$Arguments, [switch]$Unauthenticated,
          [ValidateRange(1,1800000)][int]$TimeoutMilliseconds = 60000)
    if ($Command -notin @('list','checkToolsState','runProgramInGuest','CopyFileFromHostToGuest','CopyFileFromGuestToHost','captureScreen')) { throw 'VM_COMMAND_FORBIDDEN' }
    $parameters = @('-T','ws'); $pointer = [IntPtr]::Zero
    try {
        if (-not $Unauthenticated) {
            if ($null -eq $script:vmCredential) { throw 'CREDENTIAL_REQUIRED' }
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($script:vmCredential.Password)
            $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
            $parameters += @('-gu', $script:vmCredential.UserName, '-gp', $password)
        }
        $parameters += $Command
        if ($Command -ne 'list') { $parameters += $script:vmxPath }
        $parameters += $Arguments
        Invoke-QAProcess $script:vmrunPath $parameters $TimeoutMilliseconds
    } finally {
        $password = $null; $parameters = $null
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    }
}

function Get-QABuild {
    param([string]$Directory)
    $build = Assert-QALocalPath $Directory
    if ($build -notmatch '\\artifacts\\development\\([a-f0-9]{40})$') { throw 'ARTIFACT_BOUNDARY' }
    $sha = $Matches[1]
    $manifestPath = Assert-QALocalPath (Join-Path $build 'build-manifest.json')
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.source_commit -cne $sha -or $manifest.executable.path -cne 'dist/ssearch/ssearch.exe') { throw 'ARTIFACT_MISMATCH' }
    foreach ($record in @($manifest.installer, $manifest.executable)) {
        $relative = Assert-QARelative ([string]$record.path)
        $file = Assert-QALocalPath (Join-Path $build $relative)
        if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or $record.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
            (Get-Item -LiteralPath $file).Length -ne $record.size -or (Get-QAHash $file) -ine $record.sha256) { throw 'ARTIFACT_MISMATCH' }
    }
    $proofPath = Assert-QARelative ([string]$manifest.qa.postgresql)
    $proofFile = Assert-QALocalPath (Join-Path $build $proofPath)
    $proof = Get-Content -LiteralPath $proofFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($proof.ok -ne $true -or $proof.execution_mode -ne 'frozen' -or $proof.source_commit -cne $sha) { throw 'ARTIFACT_IDENTITY_UNCONFIRMED' }
    return @{ root=$build; manifest=$manifest; source_commit=$sha }
}

function New-QAPayload {
    param([string]$Build, [string]$Output, [string]$Revision)
    $identity = Get-QABuild $Build
    if ($Revision -notmatch '^[a-f0-9]{40}$') { throw 'CHECKER_REVISION_REQUIRED' }
    $out = Assert-QALocalPath $Output
    if ((Test-Path -LiteralPath $out) -or $out.StartsWith($identity.root + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $identity.root.StartsWith($out + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'OUTPUT_BOUNDARY' }
    $sources = @{}; $prefix = 'artifacts/development/' + $identity.source_commit + '/'
    foreach ($relative in @('build-manifest.json', $identity.manifest.installer.path, $identity.manifest.qa.postgresql)) {
        $null = Assert-QARelative $relative
        $sources[$prefix + $relative] = Assert-QALocalPath (Join-Path $identity.root $relative)
    }
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push((Join-Path $identity.root 'dist\ssearch'))
    while ($pending.Count) {
        $directory = Assert-QALocalPath $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            $null = Assert-QALocalPath $item.FullName
            if ($item.PSIsContainer) { $pending.Push($item.FullName) } else {
                $relative = $item.FullName.Substring($identity.root.Length + 1).Replace('\','/')
                $null = Assert-QARelative $relative
                $sources[$prefix + $relative] = $item.FullName
            }
        }
    }
    foreach ($name in @('check_installer.ps1','check_windows_guest.ps1','check_vmware.ps1')) {
        $sources['tools/' + $name] = Assert-QALocalPath (Join-Path $PSScriptRoot $name)
    }
    [long]$total = 0
    foreach ($source in $sources.Values) { $length = (Get-Item -LiteralPath $source).Length; if ($length -gt 4GB) { throw 'PAYLOAD_LIMIT' }; $total += $length }
    if ($sources.Count -gt 100000 -or $total -gt 12GB) { throw 'PAYLOAD_LIMIT' }
    $null = New-Item -ItemType Directory -Path $out
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $archivePath = Join-Path $out 'payload.zip'
    $zip = [IO.Compression.ZipFile]::Open($archivePath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($key in ($sources.Keys | Sort-Object)) {
            $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $sources[$key], $key, [IO.Compression.CompressionLevel]::NoCompression)
        }
    } finally { $zip.Dispose() }
    $records = @(); $zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        foreach ($entry in $zip.Entries) {
            $stream = $entry.Open(); $hasher = [Security.Cryptography.SHA256]::Create()
            try { $hash = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
            finally { $stream.Dispose(); $hasher.Dispose() }
            $records += @{ path=$entry.FullName; size=$entry.Length; sha256=$hash }
        }
    } finally { $zip.Dispose() }
    foreach ($record in @($identity.manifest.installer,$identity.manifest.executable)) {
        $copy = @($records | Where-Object { $_.path -ceq ($prefix + $record.path) })
        if ($copy.Count -ne 1 -or $copy[0].size -ne $record.size -or $copy[0].sha256 -ine $record.sha256) { throw 'COPIED_ARTIFACT_MISMATCH' }
    }
    $manifest = @{ schema=1; source_commit=$identity.source_commit; checker_revision=$Revision; nonce=[Guid]::NewGuid().ToString('N'); files=$records }
    $manifestPath = Join-Path $out 'payload-manifest.json'; Write-QAJson $manifestPath $manifest
    $runner = Join-Path $out 'check_windows_guest.ps1'
    [IO.File]::Copy((Join-Path $PSScriptRoot 'check_windows_guest.ps1'), $runner, $false)
    $runnerRecord = @($records | Where-Object { $_.path -ceq 'tools/check_windows_guest.ps1' })[0]
    if ((Get-QAHash $runner) -cne $runnerRecord.sha256) { throw 'CHECKER_CHANGED_DURING_COPY' }
    return @{ directory=$out; manifest=$manifest; manifest_sha256=(Get-QAHash $manifestPath);
        archive_sha256=(Get-QAHash $archivePath); runner_sha256=(Get-QAHash $runner); status='PAYLOAD_READY' }
}

function Receive-QAEvidence {
    param([string]$GuestDirectory, [string]$LocalDirectory)
    $records = @(); $outcomes = @()
    foreach ($name in @('guest-result.json','environment.json','gui-ready.json','gui-closed.json','installer-qa.json',
        'self-test-first.json','self-test-repeat.json','postgres-seed.json','postgres-after-reinstall.json',
        'postgres-after-uninstall.json','install-first.log','install-repeat.log','uninstall.log','uninstall-after-failure.log','qt-offscreen.png')) {
        $local = Join-Path $LocalDirectory $name
        $outcome = @{ path=$name; required_on_success=($name -ne 'uninstall-after-failure.log'); status='OVERWRITE_REFUSED' }
        try {
            if (Test-Path -LiteralPath $local) { throw 'EVIDENCE_OVERWRITE_REFUSED' }
            # vmrun cannot reliably distinguish an absent guest file from other
            # transfer errors without exposing its raw, potentially secret output.
            $outcome.status = 'COPY_FAILED_OR_MISSING'
            $null = Invoke-VmCommand 'CopyFileFromGuestToHost' @((Join-Path $GuestDirectory ('evidence\' + $name)), $local)
            $outcome.status = 'MISSING_LOCAL_FILE'
            if (-not (Test-Path -LiteralPath $local -PathType Leaf)) { throw 'EVIDENCE_MISSING' }
            $outcome.status = 'SIZE_CHECK_FAILED'
            $size = (Get-Item -LiteralPath $local).Length
            $outcome.status = 'OVERSIZE'
            if ($size -gt 16MB) { throw 'EVIDENCE_LIMIT' }
            $outcome.status = 'HASH_FAILED'
            $hash = Get-QAHash $local
            if ($hash -notmatch '^[a-f0-9]{64}$') { throw 'EVIDENCE_HASH_FAILED' }
            $records += @{ path=$name; size=$size; sha256=$hash }
            $outcome.status = 'RECEIVED'
        } catch { } # Retain only the typed stage outcome, never raw transport errors.
        $outcomes += $outcome
    }
    $missing = @($outcomes | Where-Object { $_.required_on_success -and $_.status -ne 'RECEIVED' } | ForEach-Object { $_.path })
    return @{ files=$records; outcomes=$outcomes; missing_required=$missing; complete_for_success=($missing.Count -eq 0) }
}
function Invoke-QAPreflight {
    $running = Invoke-VmCommand 'list' @() -Unauthenticated
    if (-not (($running -split '[\r\n]+' | ForEach-Object { $_.Trim() }) -icontains $script:vmxPath)) { throw 'VM_NOT_RUNNING' }
    $tools = Invoke-VmCommand 'checkToolsState' @() -Unauthenticated
    if ($tools.Trim() -ne 'running') { throw 'VMWARE_TOOLS_NOT_RUNNING' }
    return @{ status='PREFLIGHT_PASS'; vmx=$script:vmxPath; tools='running'; guest_qa='DEFERRED'; native_ui_verified=$false }
}

function Invoke-VMwareQA {
    param([string]$Action, [string]$Build, [string]$Output, [string]$Revision, [string]$GuestParent,
          [ValidateRange(10,18000)][int]$TimeoutSeconds = 7200)
    if ($Action -eq 'Preflight') { return Invoke-QAPreflight }
    $payload = New-QAPayload $Build $Output $Revision
    if ($Action -eq 'Prepare') {
        Write-QAJson (Join-Path $payload.directory 'host-result.json') @{ status='PAYLOAD_READY'; source_commit=$payload.manifest.source_commit;
            checker_revision=$Revision; manifest_sha256=$payload.manifest_sha256; archive_sha256=$payload.archive_sha256;
            guest_qa='DEFERRED'; screenshots_captured=$false; native_ui_verified=$false }
        return $payload
    }
    if ($null -eq $script:vmCredential) { throw 'CREDENTIAL_REQUIRED' }
    $null = Invoke-QAPreflight
    if ($GuestParent -notmatch '^[A-Za-z]:\\Users\\[A-Za-z0-9_. -]+\\AppData\\Local\\AtomSearch-VM-QA$' -or $GuestParent.Contains("'")) { throw 'GUEST_ROOT_INVALID' }
    $guest = $GuestParent + '\run-' + $payload.manifest.nonce; $out = $payload.directory
    $returned = Join-Path $out 'returned'; $null = New-Item -ItemType Directory -Path $returned
    $probes = Join-Path $out 'probes'; $null = New-Item -ItemType Directory -Path $probes
    $result = [ordered]@{ status='FAILED'; source_commit=$payload.manifest.source_commit; checker_revision=$Revision;
        manifest_sha256=$payload.manifest_sha256; archive_sha256=$payload.archive_sha256; guest_directory=$guest;
        guest_qa='DEFERRED'; screenshots_captured=$false; native_ui_verified=$false; error_code=$null;
        process_completion='unknown'; bootstrap_receipt=$null; evidence=@(); evidence_outcomes=@(); missing_required_evidence=@(); desktop_captures=@() }
    $guestPowerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    try {
        $bootstrap = @(
            '$ErrorActionPreference="Stop"',
            ('$p="' + $GuestParent + '"; $r="' + $guest + '"'),
            '$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())',
            'if($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 31}',
            'if($p -ine (Join-Path $env:LOCALAPPDATA "AtomSearch-VM-QA")){exit 32}',
            '$c=$r; while($c){if(Test-Path -LiteralPath $c){if((Get-Item -LiteralPath $c -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){exit 33}}; $d=[IO.Directory]::GetParent($c); if(!$d){break}; $c=$d.FullName}',
            'if(Test-Path -LiteralPath $r){exit 34}',
            'if(!(Test-Path -LiteralPath $p)){New-Item -ItemType Directory -Path $p | Out-Null}',
            'New-Item -ItemType Directory -Path $r | Out-Null',
            '$bytes=[Text.Encoding]::UTF8.GetBytes((@{nonce=(Split-Path $r -Leaf).Substring(4);run=$r;created=$true}|ConvertTo-Json)); $f=[IO.File]::Open((Join-Path $r "bootstrap-ready.json"),[IO.FileMode]::CreateNew); try{$f.Write($bytes,0,$bytes.Length)}finally{$f.Dispose()}'
        ) -join "`n"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($bootstrap))
        $null = Invoke-VmCommand 'runProgramInGuest' @('-interactive',$guestPowerShell,'-NoProfile','-NonInteractive','-EncodedCommand',$encoded)
        # vmrun completion alone is not proof of successful guest initialization.
        $receiptPath = Join-Path $out 'bootstrap-ready.json'
        $null = Invoke-VmCommand 'CopyFileFromGuestToHost' @((Join-Path $guest 'bootstrap-ready.json'),$receiptPath)
        $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($receipt.nonce -cne $payload.manifest.nonce -or $receipt.run -ine $guest -or $receipt.created -ne $true) { throw 'BOOTSTRAP_RECEIPT_INVALID' }
        $result.bootstrap_receipt = @{ path='bootstrap-ready.json'; sha256=(Get-QAHash $receiptPath) }
        foreach ($name in @('payload.zip','payload-manifest.json','check_windows_guest.ps1')) {
            $null = Invoke-VmCommand 'CopyFileFromHostToGuest' @((Join-Path $out $name),(Join-Path $guest $name)) -TimeoutMilliseconds 1800000
        }
        $before = Join-Path $out 'desktop-before.png'; $null = Invoke-VmCommand 'captureScreen' @($before)
        $result.desktop_captures += @{path='desktop-before.png'; sha256=(Get-QAHash $before); kind='actual VMware desktop'}
        $startGuest = "`$ErrorActionPreference='Stop'; `$p='$guest\check_windows_guest.ps1'; if((Get-FileHash -LiteralPath `$p -Algorithm SHA256).Hash -ine '$($payload.runner_sha256)'){exit 35}; & `$p -RunDirectory '$guest' -ManifestSha256 '$($payload.manifest_sha256)' -ArchiveSha256 '$($payload.archive_sha256)'"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($startGuest))
        $null = Invoke-VmCommand 'runProgramInGuest' @('-noWait','-interactive',$guestPowerShell,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded)
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        $captured = $false; $completed = $false; $iteration = 0
        while ([DateTime]::UtcNow -lt $deadline) {
            $iteration++
            if (-not $captured) {
                $readyPath = Join-Path $probes ("ready-$iteration.json"); $captureStarted = $false
                try {
                    $null = Invoke-VmCommand 'CopyFileFromGuestToHost' @((Join-Path $guest 'evidence\gui-ready.json'),$readyPath) -TimeoutMilliseconds 10000
                    $ready = Get-Content -LiteralPath $readyPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($ready.nonce -cne $payload.manifest.nonce -or $ready.session_id -eq 0 -or $ready.window_handle -eq 0) { throw 'GUI_READY_INVALID' }
                    $captureStarted = $true
                    $screen = Join-Path $out 'desktop-installed-app.png'; $null = Invoke-VmCommand 'captureScreen' @($screen)
                    $result.desktop_captures += @{path='desktop-installed-app.png'; sha256=(Get-QAHash $screen); kind='actual VMware desktop; app readiness reported, visual review pending'}
                    $ack = Join-Path $out 'capture-ack.json'; Write-QAJson $ack @{nonce=$payload.manifest.nonce; captured=$true}
                    $null = Invoke-VmCommand 'CopyFileFromHostToGuest' @($ack,(Join-Path $guest 'evidence\capture-ack.json'))
                    $captured = $true; $result.screenshots_captured = $true
                } catch {
                    if ($_.Exception.Message -eq 'GUI_READY_INVALID') { throw }
                    if ($captureStarted) { throw 'DESKTOP_CAPTURE_FAILED_STATE_UNKNOWN' }
                }
            }
            $finishedPath = Join-Path $probes ("result-$iteration.json")
            try {
                $null = Invoke-VmCommand 'CopyFileFromGuestToHost' @((Join-Path $guest 'evidence\guest-result.json'),$finishedPath) -TimeoutMilliseconds 10000
                $finished = Get-Content -LiteralPath $finishedPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($finished.nonce -cne $payload.manifest.nonce -or $finished.source_commit -cne $payload.manifest.source_commit -or
                    $finished.manifest_sha256 -cne $payload.manifest_sha256 -or $finished.archive_sha256 -cne $payload.archive_sha256) { throw 'GUEST_RESULT_IDENTITY_INVALID' }
                $completed = $true; $result.process_completion = 'guest runner reported completion; descendants governed by lifecycle report'
                $result.guest_qa = $finished.status
                if ($finished.status -ne 'PASS' -or -not $captured) { throw 'GUEST_QA_FAILED' }
                $result.status = 'PASS'; break
            } catch { if ($_.Exception.Message -in @('GUEST_RESULT_IDENTITY_INVALID','GUEST_QA_FAILED')) { throw } }
            Start-Sleep -Seconds 2
        }
        if (-not $completed) { throw 'GUEST_TIMEOUT_STATE_UNKNOWN' }
    } catch {
        $code = $_.Exception.Message
        if ($code -notmatch '^[A-Z][A-Z0-9_]+$') { $code = 'HOST_STAGE_FAILED' }
        $result.error_code = $code
    } finally {
        $collection = Receive-QAEvidence $guest $returned
        $result.evidence = $collection.files
        $result.evidence_outcomes = $collection.outcomes
        $result.missing_required_evidence = $collection.missing_required
        # Keep the observed guest result, but acceptance also requires its reports.
        # Earlier failures retain their original error while collection continues.
        if ($result.status -eq 'PASS' -and -not $collection.complete_for_success) {
            $result.status = 'FAILED'
            $result.error_code = 'EVIDENCE_INCOMPLETE'
        }
        Write-QAJson (Join-Path $out 'host-result.json') $result
    }
    if ($result.status -ne 'PASS') { throw $result.error_code }
    return $result
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'; Set-StrictMode -Version Latest
    $script:vmrunPath = Assert-QALocalPath $Vmrun
    $script:vmxPath = if ($Vmx) { Assert-QALocalPath $Vmx } else { $null }
    $script:vmCredential = $Credential
    if ($Mode -ne 'Prepare' -and (-not $script:vmxPath -or -not (Test-Path -LiteralPath $script:vmxPath -PathType Leaf))) { throw 'EXPLICIT_EXISTING_VMX_REQUIRED' }
    try { Invoke-VMwareQA $Mode $BuildDirectory $OutputDirectory $CheckerRevision $GuestRoot $RunTimeoutSeconds | ConvertTo-Json -Depth 12 }
    finally { $script:vmCredential = $null; $Credential = $null }
}
