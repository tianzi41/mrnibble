"""下载本地 TTS 模型（MeloTTS 中文+英文，sherpa-onnx VITS 格式）。

默认使用 ModelScope 镜像，避免 GitHub Release 在国内网络环境下频繁断开。
下载支持 HTTP Range 断点续传；中断后重新运行即可继续。

用法::

    python scripts/download_tts_model.py
    python scripts/download_tts_model.py --dest <dir>
    python scripts/download_tts_model.py --source github
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEST_DEFAULT = ROOT / "models" / "tts" / "melo-zh_en"

# ModelScope 镜像仓库：与 sherpa-onnx 官方 MeloTTS 文件相同，模型提供 int8 版本。
MS_BASE = "https://www.modelscope.cn/models/AdamLee/vits-melo-tts-zh_en_copy/resolve/master"
# 官方 GitHub Release 作为备用来源。
GH_ARCHIVE = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
    "vits-melo-tts-zh_en.tar.bz2"
)

# 远端路径 → 本地路径。int8 版本体积约 51MB，落地后统一命名 model.onnx。
MS_FILES = {
    "model.int8.onnx": "model.onnx",
    "tokens.txt": "tokens.txt",
    "lexicon.txt": "lexicon.txt",
    "date.fst": "date.fst",
    "number.fst": "number.fst",
    "phone.fst": "phone.fst",
    "dict/pos_dict": "dict/pos_dict",
    "dict/hmm_model.utf8": "dict/hmm_model.utf8",
    "dict/idf.utf8": "dict/idf.utf8",
    "dict/jieba.dict.utf8": "dict/jieba.dict.utf8",
    "dict/stop_words.utf8": "dict/stop_words.utf8",
    "dict/user.dict.utf8": "dict/user.dict.utf8",
}
REQUIRED = ["model.onnx", "tokens.txt", "lexicon.txt"]
OPTIONAL = ["date.fst", "number.fst", "phone.fst", "dict"]
# ModelScope 镜像仓库的 dict 子文件偶发 500，且 sherpa-onnx MeloTTS 的
# VITS 配置本身不需要这些 jieba 辅助文件；下载时跳过，不影响中英发音。
MS_FILES = {k: v for k, v in MS_FILES.items() if not k.startswith("dict/")}

TIMEOUT = httpx.Timeout(connect=30, read=120, write=30, pool=30)
CHUNK = 4 * 1024 * 1024


def remote_size(cli: httpx.Client, url: str) -> int:
    """通过 0-0 Range 取得总大小；比 HEAD 更适配 ModelScope CDN。"""
    r = cli.get(url, headers={"Range": "bytes=0-0"})
    r.raise_for_status()
    cr = r.headers.get("content-range", "")
    if "/" in cr:
        return int(cr.rsplit("/", 1)[1])
    return int(r.headers.get("content-length") or 0)


def download_file(cli: httpx.Client, url: str, target: Path) -> None:
    """单文件 Range 下载，临时文件保留在目标旁边。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("." + target.name + ".download")
    total = remote_size(cli, url)
    done = tmp.stat().st_size if tmp.exists() else 0
    if total and done >= total:
        print(f"  已完成 {target}")
        os.replace(tmp, target)
        return

    if done:
        print(f"  续传 {target.name}：{done // 1024 // 1024}MB/{total // 1024 // 1024}MB")
    else:
        print(f"  下载 {target.name}：{total // 1024 // 1024}MB")

    with open(tmp, "ab" if done else "wb") as f:
        while not total or done < total:
            end = min(done + CHUNK - 1, total - 1) if total else done + CHUNK - 1
            last = None
            for attempt in range(1, 6):
                try:
                    with cli.stream("GET", url, headers={"Range": f"bytes={done}-{end}"}) as r:
                        if r.status_code not in (200, 206):
                            r.raise_for_status()
                        if r.status_code == 200 and done:
                            raise RuntimeError("服务器忽略 Range，拒绝拼接错位")
                        got = 0
                        for block in r.iter_bytes(1 << 20):
                            f.write(block)
                            got += len(block)
                            done += len(block)
                            if total:
                                print(f"\r    {done * 100 // total:3d}%", end="", flush=True)
                        if not got:
                            raise RuntimeError("服务器返回空数据")
                        last = None
                        break
                except Exception as exc:
                    last = exc
                    print(f"\n    分段失败 {attempt}/5：{type(exc).__name__}", flush=True)
                    time.sleep(min(3, attempt))
            if last is not None:
                raise last
    print()
    os.replace(tmp, target)


def download_modelscope(dest: Path) -> None:
    print("来源：ModelScope 镜像 AdamLee/vits-melo-tts-zh_en_copy")
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as cli:
        for remote, local in MS_FILES.items():
            download_file(cli, f"{MS_BASE}/{remote}", dest / local)


def verify(dest: Path) -> bool:
    missing = [name for name in REQUIRED if not (dest / name).exists()]
    if missing:
        print(f"✗ 缺少必需文件：{missing}")
        return False
    print(f"✓ MeloTTS 模型就位：{dest}")
    for name in REQUIRED + OPTIONAL:
        p = dest / name
        if p.exists():
            print(f"  - {name}")
        elif name in OPTIONAL:
            print(f"  ⚠ 缺少可选项：{name}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="下载本地 MeloTTS zh_en 模型")
    ap.add_argument("--dest", default=str(DEST_DEFAULT), help="模型目标目录")
    ap.add_argument("--source", choices=["modelscope", "github"], default="modelscope",
                    help="下载源，默认 ModelScope 镜像")
    args = ap.parse_args()
    dest = Path(args.dest)
    if not dest.is_absolute():
        dest = (ROOT / dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    if verify(dest):
        print("模型已存在，无需重复下载。")
        return 0

    try:
        if args.source == "modelscope":
            download_modelscope(dest)
        else:
            print(f"GitHub 压缩包备用源：{GH_ARCHIVE}")
            print("请使用 ModelScope（默认）以获得逐文件断点续传。")
            return 1
    except Exception as exc:
        print(f"✗ 下载失败：{type(exc).__name__} {exc}")
        print("部分文件会保留，重新运行即可继续。")
        return 1
    return 0 if verify(dest) else 1


if __name__ == "__main__":
    sys.exit(main())
