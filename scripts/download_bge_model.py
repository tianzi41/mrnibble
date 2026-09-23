# -*- coding: utf-8 -*-
"""下载本地语义嵌入模型 bge-small-zh-v1.5（ONNX INT8，约 24MB）。

用途：设置里把「本地嵌入引擎」选为 bge 前需要模型就位。国内走 hf-mirror 镜像，
来源 Xenova/bge-small-zh-v1.5（官方权重的量化导出，MIT 许可，与 BAAI bge-small-zh-v1.5 同权重）。

用法（项目根目录）::

    .venv\\Scripts\\python.exe scripts/download_bge_model.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "models" / "embed" / "bge-small-zh-v1.5"
MIRROR = "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main"

# (远端路径, 本地相对路径, 预期最小字节数——防半截文件)
FILES = [
    ("config.json", "config.json", 400),
    ("tokenizer.json", "tokenizer.json", 100_000),
    ("tokenizer_config.json", "tokenizer_config.json", 100),
    ("special_tokens_map.json", "special_tokens_map.json", 50),
    ("onnx/model_quantized.onnx", "onnx/model_quantized.onnx", 20_000_000),
]


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / "onnx").mkdir(exist_ok=True)
    failed = []
    for remote, local, min_size in FILES:
        dst = DEST / local
        if dst.exists() and dst.stat().st_size >= min_size:
            print(f"  已存在，跳过：{local}（{dst.stat().st_size} 字节）")
            continue
        url = f"{MIRROR}/{remote}"
        print(f"  下载 {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mrnibble/1.0"})
            with urllib.request.urlopen(req, timeout=600) as resp, \
                    open(dst, "wb") as f:
                f.write(resp.read())
            size = dst.stat().st_size
            if size < min_size:
                print(f"  ✗ {local} 只有 {size} 字节（< 预期 {min_size}），疑似不完整")
                failed.append(local)
            else:
                print(f"  ✓ {local}（{size} 字节）")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {local} 下载失败：{type(exc).__name__}: {exc}")
            failed.append(local)
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    print("模型就绪：models/embed/bge-small-zh-v1.5（约 24MB）\n"
          "到「设置 → 嵌入模型 → 本地引擎」选 bge 即启用语义检索。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
