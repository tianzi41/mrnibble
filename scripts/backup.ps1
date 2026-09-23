# MrNibble backup script: mrnibble.db + files/ + exports/ -> timestamped zip
# Usage: powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 [-Dest D:\backup] [-IncludeSecret]

param(
    [string]$Dest = "",
    [switch]$IncludeSecret
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$dataDir = Join-Path $root "data"
if (-not (Test-Path $dataDir)) { Write-Error "data dir not found: $dataDir" }

if (-not $Dest) { $Dest = Join-Path $root "backups" }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

# Ensure WAL is checkpointed so the .db file is consistent (app must be closed, or we copy WAL too)
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tmp = Join-Path $env:TEMP ("mrnibble-backup-" + $stamp)
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

Copy-Item (Join-Path $dataDir "mrnibble.db") $tmp -Force -ErrorAction SilentlyContinue
foreach ($f in @("mrnibble.db-wal", "mrnibble.db-shm")) {
    $src = Join-Path $dataDir $f
    if (Test-Path $src) { Copy-Item $src $tmp -Force }
}
if (Test-Path (Join-Path $dataDir "files"))   { Copy-Item (Join-Path $dataDir "files")   $tmp -Recurse -Force }
if (Test-Path (Join-Path $dataDir "exports")) { Copy-Item (Join-Path $dataDir "exports") $tmp -Recurse -Force }
if ($IncludeSecret -and (Test-Path (Join-Path $dataDir "secret.key"))) {
    Copy-Item (Join-Path $dataDir "secret.key") $tmp -Force
}

$zip = Join-Path $Dest ("mrnibble-backup-" + $stamp + ".zip")
Compress-Archive -Path (Join-Path $tmp "*") -DestinationPath $zip -Force
Remove-Item -Recurse -Force $tmp

Write-Host ""
Write-Host ("BACKUP OK -> " + $zip) -ForegroundColor Green
Write-Host ("Size: " + [math]::Round((Get-Item $zip).Length / 1MB, 2) + " MB")
Write-Host "Restore with: scripts\restore.ps1 -Zip <path>  (close MrNibble first)"
