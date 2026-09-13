# ZhiBan restore script: extract a backup zip back into data/
# Usage: powershell -ExecutionPolicy Bypass -File scripts\restore.ps1 -Zip D:\backup\zhiban-backup-xxxx.zip

param([Parameter(Mandatory = $true)][string]$Zip)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

if (-not (Test-Path $Zip)) { Write-Error "zip not found: $Zip" }
$dataDir = Join-Path $root "data"

Write-Host "This will OVERWRITE the current database / files / exports in:" -ForegroundColor Yellow
Write-Host "  $dataDir" -ForegroundColor Yellow
$confirm = Read-Host "Type YES to continue"
if ($confirm -ne "YES") { Write-Host "Aborted."; exit 1 }

$tmp = Join-Path $env:TEMP ("zhiban-restore-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
Expand-Archive -Path $Zip -DestinationPath $tmp -Force

if (-not (Test-Path (Join-Path $tmp "zhiban.db"))) { Write-Error "backup does not contain zhiban.db" }

if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Force -Path $dataDir | Out-Null }
foreach ($f in @("zhiban.db", "zhiban.db-wal", "zhiban.db-shm")) {
    $src = Join-Path $tmp $f
    if (Test-Path $src) { Copy-Item $src $dataDir -Force }
}
foreach ($d in @("files", "exports")) {
    $src = Join-Path $tmp $d
    if (Test-Path $src) {
        $dst = Join-Path $dataDir $d
        if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
        Copy-Item $src $dst -Recurse -Force
    }
}
if (Test-Path (Join-Path $tmp "secret.key")) { Copy-Item (Join-Path $tmp "secret.key") $dataDir -Force }

Remove-Item -Recurse -Force $tmp
Write-Host ""
Write-Host "RESTORE OK" -ForegroundColor Green
Write-Host "Note: if secret.key was NOT in the backup, saved API keys need to be re-entered."
