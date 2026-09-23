"""T05 自检：嵌入与混合检索（中文必须真实命中）。

验证：
1. 上传含「洛必达法则 / 极限 / 未定式」的中文文档，切片带向量（本地哈希嵌入）；
2. ``POST /api/retrieve`` 用「洛必达法则」「极限」分别检索都能命中；
3. 检索不存在的词命中为空；
4. 英文查询在 PDF 上命中且 hit 带整数 ``page_no``；
5. 云端嵌入配置为不可达端点时，检索仍可用（降级不报错）。

运行前启动服务：
    PYTHONPATH=src .venv/Scripts/python.exe dev/t05_check_retrieve.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dev" / "_out"
DB_PATH = ROOT / "data" / "mrnibble.db"
BASE = "http://127.0.0.1:8760"
PASSED = 0

MD_CONTENT = """# 第三章 导数与微分

## 3.2 洛必达法则

洛必达法则是求未定式极限的重要方法，适用于 0/0 型与无穷比无穷型未定式。
使用前需确认分子分母在去心邻域内可导，且分母导数不为零。

## 3.3 极限的运算

极限的运算是微积分的基础。当直接代入出现 0/0 时，可先约分或使用洛必达法则。
无穷小量与无穷大量的比较，也是极限运算的常见技巧。
"""


def check(name: str, condition: bool, extra: str = "") -> None:
    """断言并记录。"""
    global PASSED
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {extra}" if extra else ""))
    if not condition:
        raise SystemExit(f"断言失败：{name} {extra}")
    PASSED += 1


def wait_ready(client: httpx.Client, doc_id: str, timeout: float = 60.0) -> dict:
    """轮询直到解析完成。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/documents/{doc_id}").json()["data"]["document"]
        if last["status"] in ("ready", "failed"):
            return last
        time.sleep(0.4)
    return last


def retrieve(client: httpx.Client, query: str, **extra) -> dict:
    """调用检索接口。"""
    payload = {"query": query, "top_k": 8, "mode": "hybrid"}
    payload.update(extra)
    return client.post("/api/retrieve", json=payload).json()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    md_path = OUT / "洛必达讲义.md"
    md_path.write_text(MD_CONTENT, encoding="utf-8")

    client = httpx.Client(base_url=BASE, timeout=60.0)

    # 0) 强制本地嵌入（离线兜底），确保确定性。
    client.put("/api/settings", json={"embed": {"provider": "local"}})

    # 1) 上传中文文档。
    with md_path.open("rb") as fh:
        resp = client.post(
            "/api/documents/upload",
            files=[("files", (md_path.name, fh.read(), "text/markdown"))],
        )
    body = resp.json()
    check("上传中文文档 code=0", body.get("code") == 0, str(body.get("message")))
    docs_created = body["data"]["documents"]
    if docs_created:
        doc_id = docs_created[0]["id"]
    else:
        # 重复文件被跳过 → 复用库中已有同名文档（保证脚本可重复运行）。
        items = client.get("/api/documents", params={"q": "洛必达讲义"}).json()["data"]["items"]
        check("重复上传时复用已有文档", bool(items), str(body["data"].get("skipped")))
        doc_id = items[0]["id"]
    doc = wait_ready(client, doc_id)
    check("中文文档 status=ready", doc["status"] == "ready", str(doc.get("error")))

    # 2) 切片带向量。
    conn = sqlite3.connect(str(DB_PATH))
    try:
        total, with_vec = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM chunks WHERE document_id=?", (doc_id,)
        ).fetchone()
        dim_rows = conn.execute(
            "SELECT DISTINCT embedding_dim FROM chunks WHERE document_id=?", (doc_id,)
        ).fetchall()
    finally:
        conn.close()
    check("中文文档有切片", total and total > 0, f"chunks={total}")
    check("切片均带向量 BLOB", with_vec == total, f"{with_vec}/{total}")
    check("向量维度为 512", any(r[0] == 512 for r in dim_rows), str([r[0] for r in dim_rows]))

    # 3) 中文检索：两个词都要命中。
    r1 = retrieve(client, "洛必达法则")["data"]
    check("检索「洛必达法则」命中≥1", len(r1["hits"]) >= 1, f"hits={len(r1['hits'])}")
    check("命中来自目标文档", any(h["document_id"] == doc_id for h in r1["hits"]))
    check("命中含 score", all("score" in h for h in r1["hits"]), str(r1["hits"][:1]))

    r2 = retrieve(client, "极限")["data"]
    check("检索「极限」命中≥1", len(r2["hits"]) >= 1, f"hits={len(r2['hits'])}")

    r3 = retrieve(client, "量子纠缠退相干不存在的词")["data"]
    check("检索不存在的词命中为空", len(r3["hits"]) == 0, f"hits={len(r3['hits'])}")

    # 4) 英文查询在 PDF 上命中且带整数 page_no。
    docs = client.get("/api/documents").json()["data"]["items"]
    pdf_doc = next((d for d in docs if d["fmt"] == "pdf" and d["status"] == "ready"
                    and "扫描" not in d["title"]), None)
    if pdf_doc:
        r4 = retrieve(client, "limit", document_ids=[pdf_doc["id"]])["data"]
        check("英文 PDF 检索命中≥1", len(r4["hits"]) >= 1, f"hits={len(r4['hits'])}")
        pages = [h["page_no"] for h in r4["hits"]]
        check("PDF 命中带整数 page_no", all(isinstance(p, int) for p in pages), str(pages))
    else:
        print("[SKIP] 未找到可用英文 PDF（先跑 t04）")

    # 5) 云端端点不可达时检索仍可用（降级不报错）。
    client.put("/api/settings", json={"embed": {"provider": "auto",
                                                "base_url": "http://127.0.0.1:9/v1",
                                                "model": "no-such-model"}})
    r5 = retrieve(client, "洛必达法则")["data"]
    check("云端不可达时检索仍命中（降级成功）", len(r5["hits"]) >= 1, f"hits={len(r5['hits'])}")
    # 复原为本地，避免影响后续。
    client.put("/api/settings", json={"embed": {"provider": "local", "base_url": ""}})

    print("-" * 60)
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
