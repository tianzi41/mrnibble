"""把构建产物打成免安装绿色包 zip（分发用，出厂状态）。

为什么单独写脚本而不是 `Compress-Archive`：

- PowerShell 的 `Compress-Archive` 对 400MB+（含 239MB 语音模型）容易吃满内存且慢；
  这里用 ``zipfile`` 流式写入，内存占用稳定。
- 必须**排除 `data/`**：那是运行期数据目录，含用户的数据库、上传文件与
  ``secret.key``（能解密 API Key）。把带数据的目录打成分发包会泄露密钥。

用法::

    python scripts/make_package.py                      # dist5 → 啃书先生-免安装绿色包-v2.0.zip
    python scripts/make_package.py --dist dist5 --out 啃书先生.zip
    python scripts/make_package.py --dry-run            # 只统计，不写文件
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 打 zip 时跳过的目录/文件（相对包内根目录）。
# data/ 是关键：含用户数据库、上传文件与 secret.key，绝不能进分发包。
EXCLUDE_DIRS = {"data", "__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".tmp")
EXCLUDE_NAMES = {".DS_Store", "Thumbs.db", "runtime.json"}


def iter_files(src: Path):
    """遍历待打包文件，产出 ``(绝对路径, 包内相对路径)``。"""
    for dirpath, dirnames, filenames in os.walk(src):
        # 原地裁剪，os.walk 就不会进入被排除的目录
        dirnames[:] = [d for d in sorted(dirnames) if d not in EXCLUDE_DIRS]
        for name in sorted(filenames):
            if name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIXES):
                continue
            full = Path(dirpath) / name
            yield full, full.relative_to(src)


def main() -> int:
    parser = argparse.ArgumentParser(description="打免安装绿色包 zip")
    parser.add_argument("--dist", default="dist5", help="构建目录（默认 dist5）")
    parser.add_argument("--app", default="啃书先生", help="构建目录内的应用文件夹名")
    parser.add_argument("--out", default="啃书先生-免安装绿色包-v2.0.zip", help="输出 zip 路径")
    parser.add_argument("--compresslevel", type=int, default=6, help="压缩级别 0-9")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    args = parser.parse_args()

    src = Path(args.dist)
    if not src.is_absolute():
        src = ROOT / src
    src = src / args.app
    if not src.is_dir():
        print(f"✗ 构建目录不存在：{src}")
        return 1

    exe = src / f"{args.app}.exe"
    if not exe.is_file():
        print(f"✗ 未找到可执行文件：{exe}（构建可能未完成）")
        return 1

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT.parent / out      # 默认放到项目上层（与历史 zip 同级）
    if out.exists() and not args.dry_run:
        print(f"✗ 输出已存在，请先改名或删除：{out}")
        return 1

    files = list(iter_files(src))
    total = sum(f.stat().st_size for f, _ in files)
    print(f"来源：{src}")
    print(f"文件：{len(files)} 个，原始大小 {total / 1024**3:.2f} GB")
    if any(rel.parts and rel.parts[0] == "data" for _, rel in files):
        print("✗ 内部错误：data/ 未被排除，拒绝打包（会泄露密钥）")
        return 1
    if args.dry_run:
        print("（dry-run，未写文件）")
        return 0

    print(f"输出：{out}")
    started = time.time()
    written = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=args.compresslevel, allowZip64=True) as zf:
        for full, rel in files:
            # 包内统一带一层应用目录名，解压后得到单个文件夹
            zf.write(full, arcname=str(Path(args.app) / rel))
            written += 1
            if written % 200 == 0:
                pct = written * 100 // max(1, len(files))
                print(f"  {pct}%  {written}/{len(files)}", flush=True)

    size = out.stat().st_size
    print(f"完成：{out}")
    print(f"  zip {size / 1024**3:.2f} GB（压缩率 {size / max(1, total) * 100:.0f}%），"
          f"耗时 {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
