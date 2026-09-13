# ZhiBan dev server launcher (ASCII only).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\run_dev.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONIOENCODING = "utf-8"

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "venv python not found: $python"
    exit 1
}

Write-Host "Starting ZhiBan dev server ..."
& $python -m backend.main
