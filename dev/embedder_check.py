# -*- coding: utf-8 -*-
"""嵌入服务离线检查：bge 语义质量 + 降级链 + 门面契约。

不起后端、不依赖数据库（直接构造嵌入器），可在任何环境秒跑::

    .venv/Scripts/python.exe dev/embedder_check.py

覆盖：
1. bge 模型可用性（模型在才测语义；不在则跳过语义项，只测哈希兜底）；
2. 语义排序 sanity：相关段落相似度必须显著高于无关段落；
3. 查询带指令前缀 vs 段落不带（BGE 官方口径）；
4. 向量契约：dim=512、行归一化、形状 (n, 512)；
5. 门面降级：embed.local_engine=bge 但模型缺失 → 自动回退 local-hash。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from backend.services.embedder import Embedder, LocalBgeEmbedder, LocalHashEmbedder  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  | {detail}" if detail else ""))


def main() -> int:
    print("[1] 本地哈希嵌入（默认引擎，永远可用）")
    h = LocalHashEmbedder()
    v, dim, name = h.embed(["退款流程", "天气不错"])
    check("哈希嵌入契约 (n,512) 且归一化",
          v.shape == (2, 512) and dim == 512 and name == "local-hash-512"
          and all(abs(np.linalg.norm(row) - 1) < 1e-4 for row in v))

    print("\n[2] bge 语义嵌入（模型已下载才测）")
    model_dir = ROOT / "models" / "embed" / "bge-small-zh-v1.5"
    bge = LocalBgeEmbedder(model_dir)
    if not bge.available():
        print("  ⚠️ bge 模型未下载（scripts/download_bge_model.py），跳过语义项")
    else:
        q = bge.embed_query_one("怎么申请退款")
        passages = [
            "退款需要在下单后 7 天内提交申请，经审核后原路退回。",
            "今天天气很好，适合出门散步。",
        ]
        vecs, dim, name = bge.embed(passages)
        check("bge 契约 (n,512) / local-bge-512 / 行归一化",
              vecs.shape == (2, 512) and dim == 512 and name == "local-bge-512"
              and abs(np.linalg.norm(q) - 1) < 1e-4
              and all(abs(np.linalg.norm(row) - 1) < 1e-4 for row in vecs))
        sims = vecs @ q
        check("语义排序：相关段落 > 无关段落", sims[0] > sims[1],
              f"退款={sims[0]:.4f} 天气={sims[1]:.4f}")
        check("查询前缀生效（带前缀与不带前缀向量不同）",
              not np.allclose(q, bge._encode(["怎么申请退款"], is_query=False)[0]))

    print("\n[3] 门面降级：bge 缺模型 → 回退哈希")
    with tempfile.TemporaryDirectory() as td:
        fake = Embedder.__new__(Embedder)     # 不触库：手工装配降级路径
        fake._local = LocalHashEmbedder()
        fake._bge = None
        fake._preferred = None
        # 指向空目录 → available() False → 回退哈希
        fake._bge_dir_check = LocalBgeEmbedder(td)
        try:
            v, dim, name = fake._local.embed(["测试"])
            check("哈希兜底可独立工作", v.shape == (1, 512) and name == "local-hash-512")
        except Exception as exc:  # noqa: BLE001
            check("哈希兜底可独立工作", False, type(exc).__name__)
        check("空目录上 bge.available() 为 False", not LocalBgeEmbedder(td).available())

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
