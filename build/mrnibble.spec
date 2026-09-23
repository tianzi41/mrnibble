# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（架构文档 §3.1 / T14）。

用法（在项目根目录）::

    .venv\\Scripts\\pyinstaller build/mrnibble.spec --noconfirm --distpath dist

产出（--onedir 免安装绿色包）::

    dist/啃书先生/
    ├─ 啃书先生.exe
    ├─ _internal/            # = sys._MEIPASS（只读随包资源在这里）
    │  ├─ web/               # 前端（含 vendor，零 CDN）
    │  ├─ models/            # 本地语音模型（若执行过 download_models.ps1）
    │  └─ backend/db/schema.sql
    └─ data/                 # 首次运行自动生成（可写，随包拷走即迁移）
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# spec 相对路径以 spec 文件所在目录为基准，这里统一换算到项目根。
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
def _p(*parts):
    return os.path.join(ROOT, *parts)

# 让 collect_submodules 能找到 backend 包（项目以 src/ 为包根，未安装进环境）。
sys.path.insert(0, _p("src"))

datas = [
    (_p("src/web"), "web"),
    (_p("src/backend/db/schema.sql"), "backend/db"),
    # 本地语音模型随包分发：解压即可离线语音输入（R-G01/G02），无需首次联网下载。
    (_p("models"), "models"),
]
binaries = []
hiddenimports = []

# jieba 的词典是包内数据文件，必须显式收集。
datas += collect_data_files("jieba")
hiddenimports += collect_submodules("jieba")

# sherpa-onnx 的原生 DLL 由 PyInstaller 自动收集；这里补充其子模块。
hiddenimports += collect_submodules("sherpa_onnx")

# 本地语义嵌入（embed.local_engine=bge 时启用；默认关闭但依赖随包，保证开了就能用）：
# onnxruntime 的子模块 + 原生 DLL；tokenizers 是 Rust 扩展（.pyd 也要收）。
hiddenimports += collect_submodules("onnxruntime")
binaries += collect_dynamic_libs("onnxruntime")
hiddenimports += collect_submodules("tokenizers")
binaries += collect_dynamic_libs("tokenizers")

# 关键：routers/__init__.py 用 importlib 动态加载各子路由，PyInstaller 静态分析
# 看不到这些依赖，必须显式全部收集，否则打包后 chat/memory/generation/export/
# voice 等路由会被静默跳过（表现为接口 404）。
hiddenimports += collect_submodules("backend")

# uvicorn 的标准安装含 websockets/httptools 等可选加速件，按需引入。
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [_p("src/launcher.py")],
    pathex=[_p("src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="啃书先生",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 保留控制台便于排障；数据落 data/logs
    disable_windowed_traceback=False,
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="啃书先生",
)
