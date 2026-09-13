"""下载 pdf.js 到 ``src/web/vendor/pdfjs``（离线内置，运行时不访问 CDN）。

为什么用 legacy 构建：``build/pdf.mjs`` 是 ESM，只能通过 ``<script type=module>``
加载；而本项目前端是**零构建的原生脚本**（index.html 按固定顺序加载 vendor），
legacy 构建会把 ``pdfjsLib`` / ``pdfjsWorker`` 挂到 ``globalThis``，可以直接用。

用法::

    python scripts/fetch_pdfjs.py            # 已存在则跳过
    python scripts/fetch_pdfjs.py --force    # 强制重新下载
"""

from __future__ import annotations

import argparse
import io
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "src" / "web" / "vendor" / "pdfjs"

# 版本固定，避免上游更新带来的渲染差异。
VERSION = "4.7.76"
TGZ_URL = f"https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-{VERSION}.tgz"

# (包内路径, 目标文件名)
# 只取渲染所需的两个文件：界面是我们自己的（材料标注页），不需要 pdf.js 自带 viewer。
ASSETS = (
    ("package/legacy/build/pdf.min.mjs", "pdf.min.mjs"),
    ("package/legacy/build/pdf.worker.min.mjs", "pdf.worker.min.mjs"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 pdf.js 到 src/web/vendor/pdfjs")
    parser.add_argument("--force", action="store_true", help="强制重新下载")
    args = parser.parse_args()

    DEST.mkdir(parents=True, exist_ok=True)
    if not args.force and all((DEST / name).exists() for _, name in ASSETS):
        print("pdf.js 已存在，跳过（需要更新请加 --force）")
        return 0

    print(f"下载 pdfjs-dist {VERSION} …")
    with urllib.request.urlopen(TGZ_URL, timeout=120) as resp:  # noqa: S310 - 固定 npm 地址
        payload = resp.read()

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            for member, name in ASSETS:
                try:
                    handle = tar.extractfile(member)
                except KeyError:
                    print(f"  ✗ 包内缺少 {member}")
                    return 1
                if handle is None:
                    print(f"  ✗ {member} 不是普通文件")
                    return 1
                target = DEST / name
                target.write_bytes(handle.read())
                print(f"  ✓ {name}  ({target.stat().st_size // 1024} KB)")

    # worker 必须与主脚本同源同版本，否则 pdf.js 会报版本不匹配。
    print("完成。前端按 pdf.min.mjs → pdf.worker.min.mjs 顺序加载即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
