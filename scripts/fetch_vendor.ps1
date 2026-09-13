# Download frontend vendor libs to src/web/vendor (bundled locally, no CDN at runtime).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\fetch_vendor.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $root "src\web\vendor"

# NOTE on markmap: markmap-autoloader is NOT usable offline (it lazily pulls d3 +
# markmap from a CDN at runtime). We vendor the standalone bundles instead and load
# them in a fixed order in index.html: d3 -> katex -> marked -> purify -> highlight
# -> markmap-view -> markmap-lib  (markmap-lib expects window.katex to exist).
$files = @(
    @("https://cdn.jsdelivr.net/npm/marked/marked.min.js",                                  "marked\marked.min.js"),
    @("https://cdn.jsdelivr.net/npm/dompurify/dist/purify.min.js",                          "dompurify\purify.min.js"),
    @("https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets/highlight.min.js",              "highlight\highlight.min.js"),
    @("https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets/styles/github.min.css",         "highlight\github.min.css"),
    @("https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js",                                   "d3\d3.min.js"),
    @("https://cdn.jsdelivr.net/npm/markmap-view/dist/browser/index.js",                    "markmap\markmap-view.js"),
    @("https://cdn.jsdelivr.net/npm/markmap-lib/dist/browser/index.js",                     "markmap\markmap-lib.js")
)

# KaTeX ships fonts, so fetch the npm tarball and extract dist/{js,css,fonts}
$katexDest = Join-Path $vendor "katex"

function Ensure-Dir([string]$p) { New-Item -ItemType Directory -Force -Path $p | Out-Null }

foreach ($pair in $files) {
    $url = $pair[0]; $rel = $pair[1]
    $dest = Join-Path $vendor $rel
    Ensure-Dir (Split-Path -Parent $dest)
    if (Test-Path $dest) { Write-Host "skip (exists): $rel"; continue }
    Write-Host "GET $rel"
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

if (-not (Test-Path (Join-Path $katexDest "katex.min.js"))) {
    Write-Host "GET katex (tarball, includes fonts)"
    Ensure-Dir $katexDest
    $tmp = Join-Path $env:TEMP ("katex-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    Ensure-Dir $tmp
    $tgz = Join-Path $tmp "katex.tgz"
    Invoke-WebRequest -Uri "https://registry.npmmirror.com/katex/-/katex-0.18.7.tgz" -OutFile $tgz -UseBasicParsing
    tar -xzf $tgz -C $tmp
    Copy-Item (Join-Path $tmp "package\dist\katex.min.js")   $katexDest -Force
    Copy-Item (Join-Path $tmp "package\dist\katex.min.css")  $katexDest -Force
    Copy-Item (Join-Path $tmp "package\dist\fonts")          $katexDest -Recurse -Force
    Remove-Item -Recurse -Force $tmp
} else { Write-Host "skip (exists): katex" }

# pdf.js（P1：资料标注需要按页渲染 PDF）。
# 用 legacy 构建：它会把 pdfjsLib / pdfjsWorker 挂到 globalThis，
# 适配本项目「零构建、按序加载原生脚本」的前端；ESM 版需要 <script type=module>。
$pdfjsDest = Join-Path $vendor "pdfjs"
if (-not (Test-Path (Join-Path $pdfjsDest "pdf.min.mjs"))) {
    Write-Host "GET pdfjs (tarball)"
    & (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "scripts\fetch_pdfjs.py")
    if ($LASTEXITCODE -ne 0) { Write-Host "pdfjs fetch FAILED" -ForegroundColor Red }
} else { Write-Host "skip (exists): pdfjs" }

Write-Host ""
Write-Host "VENDOR OK -> $vendor" -ForegroundColor Green
