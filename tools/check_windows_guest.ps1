[CmdletBinding()]
param([string]$RunDirectory, [string]$ManifestSha256, [string]$ArchiveSha256)

function Assert-QARelative {
    param([string]$Path)
    if (-not $Path -or $Path -match '[\\:<>"|?*\x00-\x1f]' -or $Path.StartsWith('/')) { throw 'UNSAFE_RELATIVE_PATH' }
    foreach ($part in $Path.Split('/')) {
        if (-not $part -or $part -in @('.', '..') -or $part -match '[ .]$' -or
            $part -match '^(?i:CON|PRN|AUX|NUL|COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3])(?:\.|$)') { throw 'UNSAFE_RELATIVE_PATH' }
    }
    return $Path
}

function Assert-QALocalPath {
    param([string]$Path)
    if ($Path -notmatch '^[A-Za-z]:[\\/]' -or $Path -match '["\x00-\x1f]' -or $Path.Substring(2).Contains(':')) { throw 'UNSAFE_LOCAL_PATH' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'REPARSE_POINT' }
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
    return $full
}

function Get-QAHash {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-QAJson {
    param([string]$Path, $Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 12))
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}

function Expand-QAPayload {
    param([string]$Archive, [object[]]$Records, [string]$Destination)
    $destinationPath = Assert-QALocalPath $Destination
    if (Test-Path -LiteralPath $destinationPath) { throw 'EXTRACTION_DESTINATION_EXISTS' }
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $expected = @{}
        foreach ($record in $Records) {
            $key = Assert-QARelative ([string]$record.path)
            if ($expected.ContainsKey($key) -or $record.sha256 -notmatch '^[a-f0-9]{64}$' -or
                $record.size -lt 0 -or $record.size -gt 4GB) { throw 'ARCHIVE_MANIFEST_INVALID' }
            $expected[$key] = $record
        }
        $seen = @{}; [long]$total = 0
        foreach ($entry in $zip.Entries) {
            $key = Assert-QARelative $entry.FullName
            if ($seen.ContainsKey($key) -or -not $expected.ContainsKey($key) -or
                $entry.Length -ne $expected[$key].size -or
                (($entry.ExternalAttributes -band 0x400) -ne 0) -or
                (($entry.ExternalAttributes -shr 16 -band 0xF000) -eq 0xA000)) { throw 'ARCHIVE_ENTRY_INVALID' }
            $total += $entry.Length
            if ($total -gt 12GB -or $zip.Entries.Count -gt 100000) { throw 'ARCHIVE_LIMIT' }
            $stream = $entry.Open(); $hasher = [Security.Cryptography.SHA256]::Create()
            try { $hash = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
            finally { $stream.Dispose(); $hasher.Dispose() }
            if ($hash -cne $expected[$key].sha256) { throw 'ARCHIVE_HASH_MISMATCH' }
            $seen[$key] = $true
        }
        if ($seen.Count -ne $expected.Count) { throw 'ARCHIVE_MISSING_ENTRY' }
        # Validate file/directory collisions before creating anything.
        foreach ($key in $seen.Keys) {
            $parent = $key
            while ($parent.Contains('/')) {
                $parent = $parent.Substring(0, $parent.LastIndexOf('/'))
                if ($seen.ContainsKey($parent)) { throw 'ARCHIVE_PATH_COLLISION' }
            }
        }
        $null = New-Item -ItemType Directory -Path $destinationPath
        foreach ($entry in $zip.Entries) {
            $target = Join-Path $destinationPath $entry.FullName.Replace('/', '\')
            $directory = [IO.Path]::GetDirectoryName($target)
            if (-not (Test-Path -LiteralPath $directory)) { $null = New-Item -ItemType Directory -Path $directory -Force }
            $null = Assert-QALocalPath $target
            $inputStream = $entry.Open()
            $outputStream = [IO.File]::Open($target, [IO.FileMode]::CreateNew)
            try { $inputStream.CopyTo($outputStream) } finally { $inputStream.Dispose(); $outputStream.Dispose() }
        }
    } finally { $zip.Dispose() }
}

function Invoke-WindowsGuestQA {
    param([string]$Directory, [string]$ManifestHash, [string]$ArchiveHash)
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    $run = Assert-QALocalPath $Directory
    if ((Split-Path $run -Leaf) -notmatch '^run-[a-f0-9]{32}$' -or
        (Split-Path $run -Parent) -ine (Join-Path $env:LOCALAPPDATA 'AtomSearch-VM-QA')) { throw 'GUEST_RUN_BOUNDARY' }
    $manifestPath = Join-Path $run 'payload-manifest.json'
    $archivePath = Join-Path $run 'payload.zip'
    if ($ManifestHash -notmatch '^[a-f0-9]{64}$' -or $ArchiveHash -notmatch '^[a-f0-9]{64}$' -or
        (Get-QAHash $manifestPath) -cne $ManifestHash -or (Get-QAHash $archivePath) -cne $ArchiveHash) { throw 'PAYLOAD_HASH_MISMATCH' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.source_commit -notmatch '^[a-f0-9]{40}$' -or $manifest.checker_revision -notmatch '^[a-f0-9]{40}$') { throw 'PAYLOAD_IDENTITY_INVALID' }
    $evidence = Join-Path $run 'evidence'
    $null = New-Item -ItemType Directory -Path $evidence
    $result = [ordered]@{ status='FAILED'; source_commit=$manifest.source_commit; checker_revision=$manifest.checker_revision;
        manifest_sha256=$ManifestHash; archive_sha256=$ArchiveHash; nonce=$manifest.nonce;
        native_ui_verified=$false; clean_os_verified=$false; offline_verified=$false;
        qualification='Observed existing VMware guest; no clean-OS assertion'; error_code=$null; evidence_copy_failed=@() }
    $qa = $null
    try {
        $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        $session = (Get-Process -Id $PID).SessionId
        $groups = & "$env:SystemRoot\System32\whoami.exe" /groups /fo csv /nh 2>$null
        $integrity = [regex]::Match(($groups -join "`n"), 'S-1-16-\d+').Value
        $os = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
        $tools = Get-Service -Name VMTools -ErrorAction SilentlyContinue
        $environment = [ordered]@{ os_build=[string]$os.CurrentBuild; os_ubr=$os.UBR; os_product=$os.ProductName;
            x64_process=[Environment]::Is64BitProcess; x64_os=[Environment]::Is64BitOperatingSystem;
            user=[Security.Principal.WindowsIdentity]::GetCurrent().Name; integrity_sid=$integrity;
            session_id=$session; user_interactive=[Environment]::UserInteractive;
            elevated=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);
            vmware_tools_service=if ($tools) { [string]$tools.Status } else { 'not observed' };
            powershell_version=$PSVersionTable.PSVersion.ToString(); developer_tools_observed=@(); network_adapters=@() }
        foreach ($command in @('python.exe','git.exe','node.exe','cl.exe')) {
            if (Get-Command $command -ErrorAction SilentlyContinue) { $environment.developer_tools_observed += $command }
        }
        $environment.network_adapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Select-Object Name,Status)
        Write-QAJson (Join-Path $evidence 'environment.json') $environment
        if ($environment.elevated -or $integrity -ne 'S-1-16-8192') { throw 'GUEST_ORDINARY_TOKEN_REQUIRED' }
        if (-not $environment.x64_process -or -not $environment.x64_os) { throw 'GUEST_X64_REQUIRED' }
        if ($session -eq 0 -or -not $environment.user_interactive) { throw 'GUEST_INTERACTIVE_SESSION_REQUIRED' }
        if ([int]$os.CurrentBuild -lt 10240 -or [int]$os.CurrentBuild -ge 22000 -or $os.ProductName -notlike '*Windows 10*') { throw 'GUEST_WINDOWS10_REQUIRED' }
        $repo = Join-Path $run 'repository'
        Expand-QAPayload $archivePath @($manifest.files) $repo
        $build = Join-Path $repo ('artifacts\development\' + $manifest.source_commit)
        $buildManifest = Get-Content -LiteralPath (Join-Path $build 'build-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $qa = Join-Path $build 'qa\vmware-lifecycle'
        & (Join-Path $repo 'tools\check_installer.ps1') -Installer (Join-Path $build $buildManifest.installer.path) `
            -TestDir $qa -DesktopEvidenceDirectory $evidence -DesktopEvidenceNonce $manifest.nonce | Out-Null
        $summary = Get-Content -LiteralPath (Join-Path $qa 'installer-qa.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($summary.ok -ne $true) { throw 'LIFECYCLE_FAILED' }
        $result.status = 'PASS'
    } catch {
        # No raw external errors in the wrapper diagnostics. Original lifecycle
        # reports are retained separately and never contain transport credentials.
        $code = $_.Exception.Message
        if ($code -notmatch '^[A-Z][A-Z0-9_]+$') { $code = 'GUEST_STAGE_FAILED' }
        $result.error_code = $code
    } finally {
        if ($qa -and (Test-Path -LiteralPath $qa)) {
            foreach ($name in @('installer-qa.json','self-test-first.json','self-test-repeat.json',
                'postgres-seed.json','postgres-after-reinstall.json','postgres-after-uninstall.json',
                'install-first.log','install-repeat.log','uninstall.log','uninstall-after-failure.log','qt-offscreen.png')) {
                $source = Join-Path $qa $name
                if (Test-Path -LiteralPath $source -PathType Leaf) {
                    try {
                        $null = Assert-QALocalPath $source
                        if ((Get-Item -LiteralPath $source).Length -gt 16MB) { throw 'EVIDENCE_LIMIT' }
                        [IO.File]::Copy($source, (Join-Path $evidence $name), $false)
                    } catch { $result.evidence_copy_failed += $name; $result.status='FAILED'; $result.error_code='EVIDENCE_COPY_FAILED' }
                }
            }
        }
        Write-QAJson (Join-Path $evidence 'guest-result.json') $result
    }
    if ($result.status -ne 'PASS') { throw 'GUEST_QA_FAILED' }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-WindowsGuestQA $RunDirectory $ManifestSha256 $ArchiveSha256
}
