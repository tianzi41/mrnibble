# MrNibble build script (PyInstaller --onedir, portable green package).
# Usage: powershell -ExecutionPolicy Bypass -File build\build.ps1
# Requirements: .venv at project root with runtime + pyinstaller installed.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

Write-Host "[1/5] Checking venv..." -ForegroundColor Cyan
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "venv not found. Run: python -m venv .venv ; .venv\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt"
}
$py = ".venv\Scripts\python.exe"

Write-Host "[2/5] Installing dev deps (pyinstaller)..." -ForegroundColor Cyan
& $py -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
    & $py -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller | Out-Null
}

Write-Host "[3/5] Ensuring local ASR model exists..." -ForegroundColor Cyan
$modelDir = "models\asr\sense-voice-small"
$needModel = -not (Test-Path "$modelDir\model.int8.onnx")
if ($needModel) {
    Write-Host "  downloading SenseVoice int8 model (~228MB) from hf-mirror..."
    New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
    $base = "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
    & $py -c "import urllib.request,sys; urllib.request.urlretrieve('$base/tokens.txt','$modelDir\tokens.txt')"
    & $py -c "import urllib.request; urllib.request.urlretrieve('$base/model.int8.onnx','$modelDir\model.int8.onnx')"
}

$ttsDir = "models\tts\melo-zh_en"
$needTts = -not (Test-Path "$ttsDir\model.onnx")
if ($needTts) {
    Write-Host "  downloading local MeloTTS zh_en model (~159MB, resumable)..."
    & $py scripts\download_tts_model.py
    if ($LASTEXITCODE -ne 0) { Write-Error "MeloTTS model download failed" }
}

Write-Host "[4/5] Running PyInstaller..." -ForegroundColor Cyan
& $py -m PyInstaller build\mrnibble.spec --noconfirm --distpath dist --workpath build\work
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller failed" }

Write-Host "[5/5] Verifying output..." -ForegroundColor Cyan
$out = Join-Path (Resolve-Path "dist").Path "啃书先生"
if (-not (Test-Path (Join-Path $out "啃书先生.exe"))) { Write-Error "build output missing: $out" }

Write-Host ""
Write-Host ("BUILD OK -> " + $out) -ForegroundColor Green
Write-Host "Deploy: copy the whole folder to any Windows 10/11 x64 machine, then double-click 啃书先生.exe."
