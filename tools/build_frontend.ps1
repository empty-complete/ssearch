[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$CachePath)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$frontend = Join-Path $root 'frontend'
$resolvedCache = [IO.Path]::GetFullPath($CachePath)
if (!(Test-Path -LiteralPath $resolvedCache -PathType Container)) { throw 'Offline npm cache is missing. Prepare the exact locked cache in CI first.' }
Push-Location $frontend
try {
  $nodeVersion = (& node --version).Trim()
  $npmVersion = (& npm --version).Trim()
  if ($nodeVersion -ne 'v24.22.1' -or $npmVersion -ne '11.19.1') { throw 'Candidate requires Node 24.22.1 and npm 11.19.1.' }
  $cacheKeys = (& npm cache ls --cache $resolvedCache) -join "`n"
  if ($LASTEXITCODE) { throw 'Cannot read offline npm cache.' }
  $lock = Get-Content package-lock.json -Raw | ConvertFrom-Json -AsHashtable
  $platform = (& node -p 'process.platform').Trim()
  $architecture = (& node -p 'process.arch').Trim()
  foreach ($entry in $lock.packages.GetEnumerator()) {
    $package = $entry.Value
    if (!$package.resolved) { continue }
    if ($package.os -and $platform -notin $package.os) { continue }
    if ($package.cpu -and $architecture -notin $package.cpu) { continue }
    if (!$package.integrity -or !$cacheKeys.Contains($package.resolved)) { throw "Offline locked cache incomplete: $($entry.Key). No download attempted." }
  }
  & npm cache verify --cache $resolvedCache
  if ($LASTEXITCODE) { throw 'Offline cache integrity verification failed.' }
  & npm ci --offline --ignore-scripts --cache $resolvedCache --no-audit --no-fund
  if ($LASTEXITCODE) { throw 'Offline locked dependency installation failed.' }
  & npm run build
  if ($LASTEXITCODE) { throw 'Frontend production build failed.' }
} finally { Pop-Location }
