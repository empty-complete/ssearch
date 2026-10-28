[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$DesktopCachePath)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = Split-Path -Parent $PSScriptRoot
$frontend = Join-Path $root 'frontend'
$cacheRoot = Join-Path $frontend '.cache'
$receipt = Join-Path $cacheRoot 'offline-negative.json'
if (Test-Path -LiteralPath $receipt) { throw 'OFFLINE_NEGATIVE_RECEIPT_EXISTS' }
$DesktopCachePath = [IO.Path]::GetFullPath($DesktopCachePath)
$nodeModules = Join-Path $frontend 'node_modules'
$createdNodeModules = $false
$markerOwned = $false
$negativeRoot = $null
$emptyCache = $null
$marker = $null
$receipts = @()

function Assert-OrdinaryDirectory([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'OFFLINE_NEGATIVE_DIRECTORY_INVALID'
    }
}

function Test-OwnedMarker([string]$Path, [byte[]]$Expected) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item -or $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -ne $Expected.Length) {
        return $false
    }
    return [Convert]::ToBase64String([IO.File]::ReadAllBytes($Path)) -ceq [Convert]::ToBase64String($Expected)
}

Assert-OrdinaryDirectory $frontend
if (-not (Test-Path -LiteralPath $cacheRoot)) { $null = New-Item -ItemType Directory -Path $cacheRoot }
Assert-OrdinaryDirectory $cacheRoot
$token = [Text.Encoding]::UTF8.GetBytes('preserve-before-npm-ci:' + [Guid]::NewGuid().ToString('N'))
try {
    if (-not (Test-Path -LiteralPath $nodeModules)) {
        $null = New-Item -ItemType Directory -Path $nodeModules
        $createdNodeModules = $true
    }
    Assert-OrdinaryDirectory $nodeModules
    $marker = Join-Path $nodeModules ('.ssearch-offline-proof-' + [Guid]::NewGuid().ToString('N'))
    $stream = [IO.File]::Open($marker, 'CreateNew', 'Write', 'None')
    $markerOwned = $true
    try { $stream.Write($token, 0, $token.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
    $negativeRoot = Join-Path $cacheRoot ('ssearch-negative-' + [Guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $negativeRoot
    $emptyCache = Join-Path $negativeRoot 'empty'
    $null = New-Item -ItemType Directory -Path $emptyCache
    $cases = @(
        @{name='missing'; path=(Join-Path $negativeRoot 'missing'); error='Offline npm cache is missing.'},
        @{name='empty'; path=$emptyCache; error='Offline locked cache incomplete:'}
    )
    foreach ($case in $cases) {
        $message = $null
        try {
            # Exercise the entire real helper, including its before-ci cache
            # checks. A successful helper or a different error cannot qualify.
            & (Join-Path $root 'tools/build_frontend.ps1') -CachePath $case.path -DesktopCachePath $DesktopCachePath
        } catch { $message = $_.Exception.Message }
        if ($null -eq $message -or -not $message.StartsWith($case.error, [StringComparison]::Ordinal)) {
            throw 'OFFLINE_NEGATIVE_REFUSAL_MISMATCH'
        }
        if (-not (Test-OwnedMarker $marker $token)) { throw 'OFFLINE_NEGATIVE_MARKER_CHANGED' }
        if (Test-Path -LiteralPath (Join-Path $case.path '_cacache/content-v2')) {
            throw 'OFFLINE_NEGATIVE_PACKAGE_CONTENT_APPEARED'
        }
        # Only fixed public reasons are retained, never the original exception.
        $receipts += @{scenario=$case.name; rejected_before_npm_ci=$true;
            package_downloads=$false; error=$case.error}
    }
} finally {
    $cleanupConfirmed = $true
    if ($markerOwned) {
        if (Test-OwnedMarker $marker $token) {
            Remove-Item -LiteralPath $marker -ErrorAction Stop
        } else {
            # Preserve any replacement or unknown file instead of deleting it.
            $cleanupConfirmed = $false
        }
    }
    if ($createdNodeModules) {
        Assert-OrdinaryDirectory $nodeModules
        if (@([IO.Directory]::EnumerateFileSystemEntries($nodeModules)).Count -eq 0) {
            [IO.Directory]::Delete($nodeModules, $false)
        } else { $cleanupConfirmed = $false }
    }
    foreach ($directory in @($emptyCache, $negativeRoot)) {
        if ($null -ne $directory -and (Test-Path -LiteralPath $directory)) {
            Assert-OrdinaryDirectory $directory
            # npm may create diagnostic cache entries. Retain unknown contents;
            # only our own empty directories are eligible for removal.
            if (@([IO.Directory]::EnumerateFileSystemEntries($directory)).Count -eq 0) {
                [IO.Directory]::Delete($directory, $false)
            }
        }
    }
    if (-not $cleanupConfirmed) { throw 'OFFLINE_NEGATIVE_CLEANUP_UNCONFIRMED' }
}
if ($receipts.Count -ne 2) { throw 'OFFLINE_NEGATIVE_CASES_INCOMPLETE' }
$stream = [IO.File]::Open($receipt, 'CreateNew', 'Write', 'None')
try {
    $json = (ConvertTo-Json -InputObject $receipts -Depth 6) + "`n"
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush($true)
} finally { $stream.Dispose() }
