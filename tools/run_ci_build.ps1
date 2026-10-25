[CmdletBinding()]
param(
    [switch]$Child,
    [string]$ExpectedSid,
    [string]$RunDirectory,
    [ValidateRange(60, 20700)][int]$TimeoutSeconds = 19800
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Only the disposable hosted VM creates an account. Never use this launcher on
# a contributor's computer, and never relax the application's own guards.
if ($env:GITHUB_ACTIONS -cne 'true') { throw 'CI_ACTIONS_REQUIRED' }
if ($Child -and (-not $ExpectedSid -or -not $RunDirectory)) { throw 'CI_OWNED_CHILD_REQUIRED' }
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $env:GITHUB_WORKSPACE -or
    [IO.Path]::GetFullPath($env:GITHUB_WORKSPACE).TrimEnd('\') -ine $repository.TrimEnd('\')) {
    throw 'CI_WORKSPACE_MISMATCH'
}
for ($entry = Get-Item -LiteralPath $repository; $null -ne $entry; $entry = $entry.Parent) {
    if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'CI_WORKSPACE_REPARSE' }
}

function Invoke-CiChild {
    if ($ExpectedSid -notmatch '^S-1-5-21-\d+-\d+-\d+-\d+$' -or
        [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($RunDirectory)) -ine (Join-Path $repository '.local') -or
        [IO.Path]::GetFileName($RunDirectory) -notmatch '^ci-[0-9a-f]{12}$') {
        throw 'CI_OWNED_CHILD_REQUIRED'
    }
    # The parent assigns this retained process to its kill-on-close job before
    # admitting any build code or descendants. No credential is stored here.
    $admission = Join-Path $RunDirectory 'admitted.json'
    $wait = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path -LiteralPath $admission -PathType Leaf)) {
        if ($wait.Elapsed.TotalSeconds -ge 30) { throw 'CI_CHILD_NOT_ADMITTED' }
        Start-Sleep -Milliseconds 100
    }
    $ticket = Get-Content -LiteralPath $admission -Raw | ConvertFrom-Json
    if ($ticket.pid -ne $PID -or $ticket.sid -cne $ExpectedSid) { throw 'CI_CHILD_NOT_ADMITTED' }
    $tokenRights = [Security.Principal.TokenAccessLevels]::Query -bor
                   [Security.Principal.TokenAccessLevels]::Impersonate -bor
                   [Security.Principal.TokenAccessLevels]::Duplicate
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent($tokenRights)
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $groups = & "$env:SystemRoot\System32\whoami.exe" /groups /fo csv /nh
    if ($LASTEXITCODE -ne 0 -or $identity.User.Value -cne $ExpectedSid -or
        $identity.Owner.Value -cne $ExpectedSid -or
        $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or
        [regex]::Match(($groups -join "`n"), 'S-1-16-\d+').Value -cne 'S-1-16-8192') {
        throw 'CI_STANDARD_USER_REQUIRED'
    }

    # The parent requests the user's environment at process creation. Resolve
    # folders for that verified identity without inferred paths or repairs.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SsearchCiKnownFolders {
    [DllImport("shell32.dll", ExactSpelling = true)]
    static extern int SHGetKnownFolderPath(ref Guid id, uint flags, SafeHandle token, out IntPtr path);
    public static string Get(Guid id, SafeHandle token, uint flags) {
        if (token == null || token.IsInvalid || token.IsClosed)
            throw new InvalidOperationException("CI_PROFILE_TOKEN_REQUIRED");
        IntPtr path = IntPtr.Zero;
        try {
            int result = SHGetKnownFolderPath(ref id, flags, token, out path);
            if (result != 0) throw new COMException("CI_KNOWN_FOLDER_FAILED folder=" + id.ToString("D") + " hresult=" + result.ToString("X8"), result);
            if (path == IntPtr.Zero) throw new InvalidOperationException("CI_KNOWN_FOLDER_EMPTY");
            return Marshal.PtrToStringUni(path);
        } finally {
            if (path != IntPtr.Zero) Marshal.FreeCoTaskMem(path);
        }
    }
}
'@
    $token = $identity.AccessToken
    # Lexical lookup exposes only containment booleans before touching a folder.
    # DONT_VERIFY is not acceptance: flags0 verification is mandatory below.
    $profile = [SsearchCiKnownFolders]::Get([Guid]'5E6C858F-0E22-4760-9AFE-EA3317B67173', $token, 0x4000)
    $local = [SsearchCiKnownFolders]::Get([Guid]'F1B32785-6FBA-4FCF-9D55-7B8E7F157091', $token, 0x4000)
    $roaming = [SsearchCiKnownFolders]::Get([Guid]'3EB685DB-65F9-4CF6-A03A-E3EF65729F3D', $token, 0x4000)
    $profileKey = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$ExpectedSid"
    $registered = (Get-ItemProperty -LiteralPath $profileKey -Name ProfileImagePath).ProfileImagePath
    # Diagnose the boundary using only fixed predicate names and booleans:
    # never expose account identifiers, paths, environment values or credentials.
    $profileChecks = [ordered]@{
        HKU_loaded = [bool](Test-Path -LiteralPath "Registry::HKEY_USERS\$ExpectedSid")
        knownfolder_profile_nonempty = [bool]$profile
        knownfolder_local_nonempty = [bool]$local
        knownfolder_roaming_nonempty = [bool]$roaming
        registered_profile_matches = [bool]($profile -and $registered -and
            [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($registered)) -ieq $profile)
        local_within_profile = [bool]($profile -and $local -and
            $local.StartsWith($profile + '\', [StringComparison]::OrdinalIgnoreCase))
        roaming_within_profile = [bool]($profile -and $roaming -and
            $roaming.StartsWith($profile + '\', [StringComparison]::OrdinalIgnoreCase))
    }
    Write-Host ('CI loaded profile predicates: ' + ($profileChecks | ConvertTo-Json -Compress))
    if ($profileChecks.Values -contains $false) {
        throw 'CI_LOADED_PROFILE_REQUIRED'
    }
    if ([SsearchCiKnownFolders]::Get([Guid]'5E6C858F-0E22-4760-9AFE-EA3317B67173', $token, 0) -ine $profile -or
        [SsearchCiKnownFolders]::Get([Guid]'F1B32785-6FBA-4FCF-9D55-7B8E7F157091', $token, 0) -ine $local -or
        [SsearchCiKnownFolders]::Get([Guid]'3EB685DB-65F9-4CF6-A03A-E3EF65729F3D', $token, 0) -ine $roaming) {
        throw 'CI_KNOWN_FOLDER_CHANGED'
    }
    foreach ($item in @(Get-ChildItem Env:)) {
        if ($item.Name.StartsWith('PG', [StringComparison]::OrdinalIgnoreCase) -or
            $item.Name -match '^GIT_CONFIG_(KEY|VALUE)_\d+$') {
            [Environment]::SetEnvironmentVariable($item.Name, $null, 'Process')
        }
    }
    $remainingPg = @(Get-ChildItem Env: | Where-Object { $_.Name.StartsWith('PG', [StringComparison]::OrdinalIgnoreCase) })
    Write-Host ('CI PG cleanup predicates: ' + (@{
        remaining = $remainingPg.Count
        empty = @($remainingPg | Where-Object { $_.Value -eq '' }).Count
    } | ConvertTo-Json -Compress))
    $env:USERPROFILE = $profile
    $env:LOCALAPPDATA = $local
    $env:APPDATA = $roaming
    $env:HOME = $profile
    $env:USERNAME = $identity.Name.Split('\')[-1]
    $env:USERDOMAIN = $env:COMPUTERNAME
    $env:HOMEDRIVE = [IO.Path]::GetPathRoot($profile).TrimEnd('\')
    $env:HOMEPATH = $profile.Substring($env:HOMEDRIVE.Length)
    $env:TEMP = Join-Path $RunDirectory 't'
    $env:TMP = $env:TEMP
    $env:PYTEST_DEBUG_TEMPROOT = Join-Path $RunDirectory 'p'
    $null = New-Item -ItemType Directory -Path $env:TEMP, $env:PYTEST_DEBUG_TEMPROOT
    $env:PYTEST_ADDOPTS = '-p no:cacheprovider --durations=20 -W error::RuntimeWarning'
    $env:PYTEST_PLUGINS = $null
    $env:QT_QPA_PLATFORM = 'offscreen'
    $env:PYTHONNOUSERSITE = '1'
    $env:PYTHONPATH = (Join-Path $repository 'src') + ';' + (Join-Path $repository 'packages/atom-search-core/src')
    # Checkout is owned by the runner account. Trust this one checked workspace
    # for this process and descendants, never all repositories or a global file.
    $env:GIT_CONFIG_COUNT = '1'
    $env:GIT_CONFIG_KEY_0 = 'safe.directory'
    $env:GIT_CONFIG_VALUE_0 = $repository
    Set-Location -LiteralPath $repository
    $python = Join-Path $repository '.venv/Scripts/python.exe'
    $preflight = @'
import os
from pathlib import Path
import sys
from atom_search.runtime import _windows
from atom_search.runtime.credentials import CredentialStore
pg_values = [value for name, value in os.environ.items() if name.upper().startswith('PG')]
print(f'CI Python PG predicates: remaining={len(pg_values)} empty={sum(value == "" for value in pg_values)}', flush=True)
assert not pg_values
profile = Path(sys.argv[1])
store = CredentialStore(profile, 'ci-preflight')
reference = store.create('ci-preflight')
assert len(store.resolve(reference)) >= 40
with _windows.mutex('ci-preflight:' + str(profile.resolve())):
    pass
store.remove(reference)
print('CI preflight: standard_user=true loaded_profile=true clean_pg_environment=true private_mutex=true dpapi=true')
'@
    & $python -c $preflight (Join-Path $RunDirectory 'preflight')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $targets = ConvertFrom-Json -InputObject $env:TEST_TARGETS_JSON -NoEnumerate
    if ($targets -isnot [array] -or @($targets | Where-Object { $_ -isnot [string] }).Count -or
        $env:BUILD_MODE -notin @('full', 'quick') -or
        ($env:BUILD_MODE -eq 'full' -and $targets.Count -ne 0) -or
        ($env:BUILD_MODE -eq 'quick' -and $targets.Count -eq 0)) { throw 'CI_BUILD_SCOPE_INVALID' }
    $parameters = @{ Development = $true; Python = $python; ISCC = (Join-Path $repository '.tools/InnoSetup7/ISCC.exe') }
    if ($env:BUILD_MODE -eq 'quick') { $parameters.Quick = $true; $parameters.TestTargets = [string[]]$targets }
    & (Join-Path $repository 'tools/build.ps1') @parameters
    # A build exception stops here. Export reads private proofs under the same
    # user and writes only the existing public allowlist for upload-artifact.
    $arguments = @((Join-Path $repository 'tools/export_ci_artifacts.py'), '--commit', $env:SOURCE_SHA)
    if ($env:BUILD_MODE -eq 'quick') { $arguments += '--quick' }
    & $python @arguments
    exit $LASTEXITCODE
}

if ($Child) {
    try { Invoke-CiChild } catch { Write-Error $_ -ErrorAction Continue; exit 1 }
}

$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'CI_PARENT_ADMIN_REQUIRED'
}
# A job owns just this child and its descendants. Closing it cannot target a
# pre-existing PostgreSQL process or any other user's process tree.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
public sealed class SsearchCiJob : IDisposable {
    [StructLayout(LayoutKind.Sequential)] struct Basic {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
        public uint ActiveLimit;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct Io { public ulong A, B, C, D, E, F; }
    [StructLayout(LayoutKind.Sequential)] struct Limits {
        public Basic Basic; public Io Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [DllImport("kernel32", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);
    [DllImport("kernel32", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, int kind, ref Limits value, uint size);
    [DllImport("kernel32", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32", SetLastError=true)]
    static extern bool QueryInformationJobObject(IntPtr job, int kind, [Out] byte[] value, uint size, IntPtr returned);
    [DllImport("kernel32", SetLastError=true)] static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);
    IntPtr handle;
    public SsearchCiJob() {
        handle = CreateJobObjectW(IntPtr.Zero, null);
        if (handle == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        var limits = new Limits(); limits.Basic.Flags = 0x2000;
        if (!SetInformationJobObject(handle, 9, ref limits, (uint)Marshal.SizeOf<Limits>())) {
            var error = Marshal.GetLastWin32Error(); Dispose(); throw new Win32Exception(error);
        }
    }
    public void Assign(Process process) {
        if (!AssignProcessToJobObject(handle, process.SafeHandle.DangerousGetHandle()))
            throw new Win32Exception(Marshal.GetLastWin32Error());
    }
    public void Terminate() {
        if (!TerminateJobObject(handle, 1)) throw new Win32Exception(Marshal.GetLastWin32Error());
    }
    public uint ActiveProcesses {
        get {
            var data = new byte[48];
            if (!QueryInformationJobObject(handle, 1, data, (uint)data.Length, IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return BitConverter.ToUInt32(data, 40);
        }
    }
    public void Dispose() {
        if (handle == IntPtr.Zero) return;
        if (!CloseHandle(handle)) throw new Win32Exception(Marshal.GetLastWin32Error());
        handle = IntPtr.Zero;
    }
}
'@

$account = $null
$process = $null
$job = $null
$assigned = $false
$childStopped = $true
$treeDrained = $false
$startedAt = $null
$password = $null
$credential = $null
$readers = @()
$exitCode = 1
$accountName = 'ssci' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
try {
    $password = ConvertTo-SecureString ([Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48)) + 'aA1!') -AsPlainText -Force
    $account = New-LocalUser -Name $accountName -Password $password -AccountNeverExpires -PasswordNeverExpires
    Add-LocalGroupMember -SID ([Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')) -Member $account
    $credential = [PSCredential]::new("$env:COMPUTERNAME\$accountName", $password)
    $run = Join-Path $repository ('.local/ci-' + [Guid]::NewGuid().ToString('N').Substring(0, 12))
    $null = New-Item -ItemType Directory -Path $run
    # Grant only this checkout, never change product profile owners or DACLs.
    if (Get-ChildItem -LiteralPath $repository -Force -Recurse |
        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } | Select-Object -First 1) {
        throw 'CI_WORKSPACE_REPARSE'
    }
    & "$env:SystemRoot\System32\icacls.exe" $repository /grant "*$($account.SID.Value):(OI)(CI)M" /T /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'CI_WORKSPACE_ACCESS_FAILED' }
    $job = [SsearchCiJob]::new()
    # No caller environment is passed at creation. Transfer only public CI
    # context afterward, in this same process, before the normal child entry.
    $bootstrapPath = Join-Path $run 'bootstrap.ps1'
    $bootstrapData = @{
        sid = $account.SID.Value; run = $run
        context = @{
            GITHUB_ACTIONS = 'true'; GITHUB_WORKSPACE = $repository
            SOURCE_SHA = $env:SOURCE_SHA; BUILD_MODE = $env:BUILD_MODE
            TEST_TARGETS_JSON = $env:TEST_TARGETS_JSON; PYTHONUTF8 = '1'
            PIP_DISABLE_PIP_VERSION_CHECK = '1'
        }
    }
    $bootstrapText = @'
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
try {
    $data = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'launch.json') -Raw | ConvertFrom-Json
    foreach ($name in @('GITHUB_ACTIONS', 'GITHUB_WORKSPACE', 'SOURCE_SHA', 'BUILD_MODE',
                        'TEST_TARGETS_JSON', 'PYTHONUTF8', 'PIP_DISABLE_PIP_VERSION_CHECK')) {
        [Environment]::SetEnvironmentVariable($name, [string]$data.context.$name, 'Process')
    }
} catch {
    Write-Error ('CI_BOOTSTRAP_FAILED hresult=' + $_.Exception.HResult.ToString('X8')) -ErrorAction Continue
    exit 1
}
& (Join-Path $env:GITHUB_WORKSPACE 'tools/run_ci_build.ps1') -Child -ExpectedSid $data.sid -RunDirectory $data.run
exit $LASTEXITCODE
'@
    try {
        [IO.File]::WriteAllText((Join-Path $run 'launch.json'), ($bootstrapData | ConvertTo-Json -Depth 3 -Compress))
        [IO.File]::WriteAllText($bootstrapPath, $bootstrapText)
    } catch {
        throw ('CI_BOOTSTRAP_WRITE_FAILED hresult=' + $_.Exception.HResult.ToString('X8'))
    }
    $launch = @{
        FilePath = (Get-Process -Id $PID).Path
        ArgumentList = @('-NoProfile', '-NonInteractive', '-File', ('"' + $bootstrapPath + '"'))
        Credential = $credential; LoadUserProfile = $true; WindowStyle = 'Hidden'; PassThru = $true
        UseNewEnvironment = $true
        WorkingDirectory = $repository
        RedirectStandardOutput = (Join-Path $run 'stdout.log')
        RedirectStandardError = (Join-Path $run 'stderr.log')
    }
    if ($launch.FilePath.Length + 4 + ($launch.ArgumentList -join ' ').Length -ge 1024) {
        throw 'CI_BOOTSTRAP_COMMAND_TOO_LONG'
    }
    $process = Start-Process @launch
    $childStopped = $false
    $startedAt = $process.StartTime.ToUniversalTime()
    $job.Assign($process)
    $assigned = $true
    $ticket = @{ pid = $process.Id; sid = $account.SID.Value } | ConvertTo-Json -Compress
    Set-Content -LiteralPath (Join-Path $run 'admitted.tmp') -Value $ticket -Encoding utf8
    Move-Item -LiteralPath (Join-Path $run 'admitted.tmp') -Destination (Join-Path $run 'admitted.json')
    $credential = $null
    $launch.Credential = $null
    $password.Dispose()
    $password = $null
    foreach ($name in @('stdout.log', 'stderr.log')) {
        $stream = [IO.File]::Open((Join-Path $run $name), 'Open', 'Read', [IO.FileShare]::ReadWrite)
        $readers += [IO.StreamReader]::new($stream)
    }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (-not $process.WaitForExit(1000)) {
        foreach ($reader in $readers) { while (-not $reader.EndOfStream) { Write-Host $reader.ReadLine() } }
        if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            $exitCode = 124
            throw 'CI_CHILD_TIMEOUT'
        }
    }
    $childStopped = $true
    foreach ($reader in $readers) { while (-not $reader.EndOfStream) { Write-Host $reader.ReadLine() } }
    $exitCode = $process.ExitCode
    $process.Dispose()
    $process = $null
    $drain = [Diagnostics.Stopwatch]::StartNew()
    while ($job.ActiveProcesses -gt 0) {
        if ($drain.Elapsed.TotalSeconds -ge 15) { throw 'CI_CHILD_TREE_NOT_DRAINED' }
        Start-Sleep -Milliseconds 100
    }
    $treeDrained = $true
} catch {
    if ($exitCode -eq 0) { $exitCode = 1 }
    Write-Error $_ -ErrorAction Continue
} finally {
    foreach ($reader in $readers) { $reader.Dispose() }
    if ($job) {
        try {
            if ($job.ActiveProcesses -gt 0) { $job.Terminate() }
        } catch {
            Write-Warning 'CI owned job cleanup could not be confirmed.'
            if ($exitCode -eq 0) { $exitCode = 1 }
        }
    }
    if ($process -and -not $process.HasExited) {
        try {
            # Before admission the child has no descendants. After assignment,
            # the retained job above terminates only its owned tree.
            if (-not $assigned -and $startedAt -and $process.StartTime.ToUniversalTime() -eq $startedAt) { $process.Kill() }
            if (-not $process.WaitForExit(15000)) { throw 'CI_CHILD_CLEANUP_UNRESOLVED' }
            $childStopped = $true
        } catch {
            Write-Warning 'CI owned child cleanup could not be confirmed.'
            if ($exitCode -eq 0) { $exitCode = 1 }
        }
    }
    if ($process -and $process.HasExited) { $childStopped = $true }
    if ($process) { $process.Dispose() }
    if ($job) {
        try {
            $drain = [Diagnostics.Stopwatch]::StartNew()
            while ($job.ActiveProcesses -gt 0) {
                if ($drain.Elapsed.TotalSeconds -ge 15) { throw 'CI_CHILD_CLEANUP_UNRESOLVED' }
                Start-Sleep -Milliseconds 100
            }
            $treeDrained = $true
        } catch {
            Write-Warning 'CI owned job drain could not be confirmed.'
            if ($exitCode -eq 0) { $exitCode = 1 }
        } finally {
            try { $job.Dispose() } catch {
                Write-Warning 'CI owned job handle cleanup could not be confirmed.'
                if ($exitCode -eq 0) { $exitCode = 1 }
            }
        }
    }
    if ($account -and $childStopped -and (-not $assigned -or $treeDrained)) {
        try {
            $current = Get-LocalUser -Name $accountName
            if ($current.SID.Value -cne $account.SID.Value) { throw 'CI_ACCOUNT_IDENTITY_CHANGED' }
            Remove-LocalUser -SID $account.SID
        } catch {
            Write-Warning 'CI temporary account cleanup could not be confirmed.'
            if ($exitCode -eq 0) { $exitCode = 1 }
        }
    }
    if ($password) { $password.Dispose() }
    $credential = $null
}
exit $exitCode
