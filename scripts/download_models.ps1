# Download MrNibble offline models to the project directory (Q drive). ASCII only.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\download_models.ps1 [-WhatIf]
#
# NOTE: This stage (T01-T05) does NOT include an embedding model -- the local
#       embedding fallback is a pure-numpy LocalHashEmbedder (zero download).
#       This script prepares the ASR (sherpa-onnx SenseVoice) model used by T11.
param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$models = Join-Path $root "models"
$asrDir = Join-Path $models "asr\sense-voice-small"

Write-Host "== MrNibble model download =="
Write-Host "Target dir: $asrDir"

if ((Test-Path (Join-Path $asrDir "tokens.txt")) -and (Test-Path (Join-Path $asrDir "model.int8.onnx"))) {
    Write-Host "ASR model already present; skip download."
    exit 0
}

# SenseVoice-small int8 (sherpa-onnx), ~228MB.
# IMPORTANT: use the HuggingFace CN mirror -- github.com release downloads are
# unreachable on typical CN networks (verified: direct + proxy both fail).
$base = "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
$files = @(
    "model.int8.onnx",
    "tokens.txt"
)

New-Item -ItemType Directory -Force -Path $asrDir | Out-Null

foreach ($f in $files) {
    $url = "$base/$f"
    $dest = Join-Path $asrDir $f
    Write-Host "  GET $f"
    if ($WhatIf) { continue }
    try {
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    } catch {
        Write-Warning "Download failed for $f : $($_.Exception.Message)"
        Write-Warning "Place the model manually under: $asrDir"
        exit 2
    }
}

Write-Host "Done. ASR model ready at: $asrDir"
