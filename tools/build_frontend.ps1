[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$CachePath,
  [Parameter(Mandatory=$true)][string]$DesktopCachePath
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$frontend = Join-Path $root 'frontend'
$nodeVersion = (& node --version).Trim()
$npmVersion = (& npm --version).Trim()
if ($nodeVersion -ne 'v24.22.1' -or $npmVersion -ne '11.19.1') {
  throw 'Candidate requires Node 24.22.1 and npm 11.19.1.'
}

function Install-LockedOffline([string]$Project, [string]$Cache) {
  $resolvedCache = [IO.Path]::GetFullPath($Cache)
  if (!(Test-Path -LiteralPath $resolvedCache -PathType Container)) {
    throw 'Offline npm cache is missing. Prepare the exact locked cache in CI first.'
  }
  Push-Location (Join-Path $root $Project)
  try {
    $cacheKeys = (& npm cache ls --cache $resolvedCache) -join "`n"
    if ($LASTEXITCODE) { throw 'Cannot read offline npm cache.' }
    $lock = Get-Content package-lock.json -Raw | ConvertFrom-Json -AsHashtable
    $platform = (& node -p 'process.platform').Trim()
    $architecture = (& node -p 'process.arch').Trim()
    $packages = @()
    foreach ($entry in $lock.packages.GetEnumerator()) {
      $package = $entry.Value
      if (!$package.ContainsKey('resolved') -or !$package['resolved']) { continue }
      if ($package.ContainsKey('os') -and $package['os'] -and $platform -notin $package['os']) { continue }
      if ($package.ContainsKey('cpu') -and $package['cpu'] -and $architecture -notin $package['cpu']) { continue }
      if (!$package.ContainsKey('integrity') -or !$package['integrity'] -or !$cacheKeys.Contains($package['resolved'])) {
        throw "Offline locked cache incomplete: $($entry.Key). No download attempted."
      }
      $packages += @{ package=$entry.Key; integrity=$package['integrity'] }
    }
    & npm cache verify --cache $resolvedCache | Out-Host
    if ($LASTEXITCODE) { throw 'Offline cache integrity verification failed.' }
    & npm ci --offline --ignore-scripts --cache $resolvedCache --no-audit --no-fund | Out-Host
    if ($LASTEXITCODE) { throw 'Offline locked dependency installation failed.' }
    return @{
      project=$Project
      lock_sha256=(Get-FileHash package-lock.json -Algorithm SHA256).Hash.ToLowerInvariant()
      cache_integrity_verified=$true
      offline_npm_ci=$true
      install_scripts=$false
      packages=$packages
    }
  } finally { Pop-Location }
}

# npm progress output is forwarded to the host, leaving only receipts as values.
$frontendProof = Install-LockedOffline -Project 'frontend' -Cache $CachePath
$desktopProof = Install-LockedOffline -Project 'desktop' -Cache $DesktopCachePath
$proof = @{
  source_sha=(& git -C $root rev-parse HEAD).Trim()
  node=$nodeVersion
  npm=$npmVersion
  projects=@($frontendProof,$desktopProof)
}
$proof | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $frontend '.cache/offline-dependencies.json')
Push-Location $frontend
try {
  & npm run build
  if ($LASTEXITCODE) { throw 'Frontend production build failed.' }
} finally { Pop-Location }
