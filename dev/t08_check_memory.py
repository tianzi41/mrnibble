"""T08 自检：长期记忆与硬删除（CRUD + 删后不可召回）。

前置：先启动主服务(8760)。

判据：
1. CRUD 全通（建/列/改/（批量）删/清空）；
2. `DELETE` 后 `memories` 与 `memories_fts` 均无该行（`SELECT COUNT(*) FROM memories_fts WHERE rowid=? = 0`）；
3. 召回接口在删除后不再返回该内容（R-E03）；
4. 导出 json/md/csv 含全部现存条目（R-E04）。

运行：
    PYTHONPATH=src .venv/Scripts/python.exe dev/t08_check_memory.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.services.memory import get_memory_service  # noqa: E402

DB_PATH = ROOT / "data" / "mrnibble.db"
BASE = "http://127.0.0.1:8760"
PASSED = 0

UNIQUE = "啃书先生记忆唯一标记样本"  # 用于精确断言删除后不可召回


def check(name: str, condition: bool, extra: str = "") -> None:
    """断言并记录。"""
    global PASSED
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {extra}" if extra else ""))
    if not condition:
        raise SystemExit(f"断言失败：{name} {extra}")
    PASSED += 1


def db_scalar(sql: str, params: tuple = ()) -> int:
    """直连 DB 取单个整数。"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30.0)
    svc = get_memory_service()

    # ── 建 ───────────────────────────────────────────────
    body = client.post(
        "/api/memories", json={"type": "knowledge_gap", "content": f"{UNIQUE}：极限的 ε-δ 定义理解不足"}
    ).json()
    mid = body["data"]["memory"]["id"]
    check("新建记忆", bool(mid), mid)
    rowid = db_scalar("SELECT rowid FROM memories WHERE id = ?", (mid,))
    check("DB 中存在该行", rowid > 0)

    # ── 列 ───────────────────────────────────────────────
    items = client.get("/api/memories", params={"type": "knowledge_gap"}).json()["data"]["items"]
    check("列表包含新记忆", any(m["id"] == mid for m in items))
    total = client.get("/api/memories").json()["data"]["total"]
    check("列表返回 total", total >= 1, str(total))

    # ── 改 ───────────────────────────────────────────────
    patched = client.patch(f"/api/memories/{mid}", json={"content": f"{UNIQUE}：ε-δ 定义已基本掌握"}).json()
    check("更新记忆内容", "已基本掌握" in patched["data"]["memory"]["content"], patched["data"]["memory"]["content"])

    # ── 召回命中（服务端召回接口，主表为准）───────────────
    hits = svc.recall(UNIQUE, top_k=5)
    check("删除前召回命中", any(h["id"] == mid for h in hits), str([h["id"][:8] for h in hits]))

    # ── 导出 ─────────────────────────────────────────────
    jin = client.get("/api/memories/export", params={"format": "json"})
    check("导出 json 含条目", UNIQUE in jin.text, jin.headers.get("content-type", ""))
    mdin = client.get("/api/memories/export", params={"format": "md"})
    check("导出 md 含条目", UNIQUE in mdin.text)
    csvout = client.get("/api/memories/export", params={"format": "csv"})
    check("导出 csv 含条目", UNIQUE in csvout.text)

    # ── 硬删除（一票否决项）──────────────────────────────
    dl = client.delete(f"/api/memories/{mid}").json()
    check("删除接口返回 deleted", dl["data"]["deleted"] is True)
    check("memories 主表无该行", db_scalar("SELECT COUNT(*) FROM memories WHERE id = ?", (mid,)) == 0)
    check("memories_fts 无该行", db_scalar("SELECT COUNT(*) FROM memories_fts WHERE rowid = ?", (rowid,)) == 0)
    hits_after = svc.recall(UNIQUE, top_k=5)
    check("删除后不可召回", all(h["id"] != mid for h in hits_after), str([h["id"][:8] for h in hits_after]))

    # ── 批量删除与清空 ───────────────────────────────────
    ids = [
        client.post("/api/memories", json={"type": "fact", "content": f"{UNIQUE}A"}).json()["data"]["memory"]["id"],
        client.post("/api/memories", json={"type": "fact", "content": f"{UNIQUE}B"}).json()["data"]["memory"]["id"],
    ]
    bulk = client.post("/api/memories/bulk_delete", json={"ids": ids}).json()
    check("批量删除 2 条", bulk["data"]["deleted_count"] == 2, str(bulk["data"]))
    check("批量删除后主表无残留", db_scalar("SELECT COUNT(*) FROM memories WHERE id IN (?, ?)", tuple(ids)) == 0)

    cleared = client.delete("/api/memories").json()
    check("清空返回计数", cleared["data"]["deleted_count"] >= 0, str(cleared["data"]))
    check("清空后主表为空", db_scalar("SELECT COUNT(*) FROM memories") == 0)
    check("清空后 FTS 为空", db_scalar("SELECT COUNT(*) FROM memories_fts") == 0)

    print("-" * 60)
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
