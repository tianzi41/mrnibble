"""T06 自检：LLM 客户端与问答编排（含引用回填、材料边界、防伪）。

前置：先启动 mock LLM(8761) 与主服务(8760)。

判据：
1. `POST /api/chat/stream` 依次收到 meta → delta → citation → done；
2. 回答中 `[n]` 的 page_no 等于对应 chunk 的 DB 值；
3. 越界 `[[c:999]]` 被丢弃，不产生任何引用，正文不含 `[[c:`;
4. 材料外问题回「材料中未提及」且 citations=[]、grounded=false；
5. 会话 CRUD 正常。

运行：
    PYTHONPATH=src .venv/Scripts/python.exe dev/t06_check_chat.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dev" / "_out"
DB_PATH = ROOT / "data" / "zhiban.db"
BASE = "http://127.0.0.1:8760"
MOCK_BASE = "http://127.0.0.1:8761/v1"
PASSED = 0

DOC = """# 微积分重点

## 洛必达法则

洛必达法则是求未定式极限的重要方法，适用于 0/0 型与无穷比无穷型未定式。
使用前必须确认分子分母在去心邻域内可导，且分母导数不为零。

## 极限的定义

极限刻画函数在趋近过程中的变化趋势，是微积分的基础概念。
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


def upload_doc(client: httpx.Client, name: str, content: str) -> str:
    """上传（或复用）一份 Markdown 文档，返回 doc_id。"""
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(content, encoding="utf-8")
    body = client.post(
        "/api/documents/upload",
        files=[("files", (name, path.read_bytes(), "text/markdown"))],
    ).json()
    docs = body["data"]["documents"]
    if docs:
        doc_id = docs[0]["id"]
    else:
        items = client.get("/api/documents", params={"q": name}).json()["data"]["items"]
        doc_id = items[0]["id"]
    wait_ready(client, doc_id)
    return doc_id


def parse_sse(client: httpx.Client, payload: dict) -> list[tuple[str, dict]]:
    """消费 SSE，返回 [(event, data), ...]。"""
    events: list[tuple[str, dict]] = []
    with client.stream("POST", "/api/chat/stream", json=payload) as resp:
        event = None
        for raw in resp.iter_lines():
            line = raw.strip()
            if not line:
                event = None
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
                if event:
                    events.append((event, data))
    return events


def db_page(doc_less: str) -> int | None:
    """按 chunk_id 查 DB page_no。"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT page_no FROM chunks WHERE id = ?", (doc_less,)).fetchone()
        return None if row is None else row["page_no"]
    finally:
        conn.close()


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=90.0)

    # 配置 LLM 指向 mock。
    client.put("/api/settings", json={
        "llm": {"base_url": MOCK_BASE, "model": "mock-normal", "api_key": "sk-mock-1234567890"}
    })

    doc_id = upload_doc(client, "T06微积分.md", DOC)

    # ── 1) 事件顺序 + 引用回填 ──────────────────────────
    events = parse_sse(client, {"message": "洛必达法则的适用条件是什么？", "document_ids": [doc_id]})
    names = [e for e, _ in events]
    check("SSE 含 meta", "meta" in names)
    check("SSE 含 delta", "delta" in names)
    check("SSE 含 citation", "citation" in names)
    check("SSE 含 done", "done" in names)
    check("顺序 meta→delta→citation→done",
          names.index("meta") < names.index("delta") < names.index("citation") < names.index("done"),
          str(names))

    answer = "".join(d["text"] for e, d in events if e == "delta")
    cit_events = [d for e, d in events if e == "citation"]
    citations = cit_events[-1]["citations"] if cit_events else []
    check("回答非空", bool(answer.strip()), answer[:40])
    check("回答不含原始标记 [[c:", "[[c:" not in answer, answer[:60])
    check("引用非空（材料内问题）", len(citations) >= 1, str(len(citations)))
    check("正文含展示角标 [n]", "[" in answer)

    # page_no 必须等于 DB 值
    ok_pages = all(c["page_no"] == db_page(c["chunk_id"]) for c in citations)
    check("引用 page_no 等于 chunk 的 DB 值", ok_pages, str([(c["n"], c["page_no"]) for c in citations]))

    # ── 2) 越界引用被丢弃 ──────────────────────────────
    client.put("/api/settings", json={"llm": {"model": "mock-fake-cite"}})
    events2 = parse_sse(client, {"message": "极限的定义是什么？", "document_ids": [doc_id]})
    answer2 = "".join(d["text"] for e, d in events2 if e == "delta")
    cit2 = [d for e, d in events2 if e == "citation"][-1]["citations"]
    check("越界 [[c:999]] 被丢弃（无 n=999 引用）", all(c["n"] != 999 for c in cit2), str(cit2))
    check("正文不含 [[c:", "[[c:" not in answer2)
    check("合法 [[c:1]] 仍被保留", any(c["n"] == 1 for c in cit2), str([c["n"] for c in cit2]))

    # ── 3) 材料外问题 → 未提及 且零引用 ──────────────────
    client.put("/api/settings", json={"llm": {"model": "mock-normal"}})
    events3 = parse_sse(client, {"message": "请解释量子纠缠与贝尔不等式的关系", "document_ids": [doc_id]})
    answer3 = "".join(d["text"] for e, d in events3 if e == "delta")
    cit3 = [d for e, d in events3 if e == "citation"][-1]["citations"]
    done3 = [d for e, d in events3 if e == "done"][-1]
    check("材料外问题含「未提及」", "未提及" in answer3, answer3[:60])
    check("材料外问题 citations=[]", cit3 == [], str(cit3))
    check("材料外问题 grounded=false", done3.get("grounded") is False, str(done3))

    # ── 4) 命中非空但模型无引用 → 强制边界提示 ───────────
    client.put("/api/settings", json={"llm": {"model": "mock-no-cite"}})
    events4 = parse_sse(client, {"message": "极限的定义是什么？", "document_ids": [doc_id]})
    answer4 = "".join(d["text"] for e, d in events4 if e == "delta")
    done4 = [d for e, d in events4 if e == "done"][-1]
    check("无引用回答被强制加「未提及」提示", "未提及" in answer4, answer4[:80])
    check("无引用回答 grounded=false", done4.get("grounded") is False)

    # ── 5) 会话 CRUD ────────────────────────────────────
    conv = client.post("/api/conversations", json={"title": "期末速成"}).json()["data"]["conversation"]
    check("新建会话", bool(conv["id"]))
    rn = client.patch(f"/api/conversations/{conv['id']}", json={"title": "期末速成-改"}).json()
    check("重命名会话", rn["data"]["conversation"]["title"] == "期末速成-改")
    listed = client.get("/api/conversations").json()["data"]["items"]
    check("会话出现在列表", any(c["id"] == conv["id"] for c in listed))
    detail = client.get(f"/api/conversations/{conv['id']}").json()["data"]["conversation"]
    check("会话详情含 messages 字段", "messages" in detail)
    dl = client.delete(f"/api/conversations/{conv['id']}").json()
    check("删除会话", dl["data"]["deleted"] is True)

    # 非流式接口（回到 mock-normal，确保真的产生引用）
    client.put("/api/settings", json={"llm": {"model": "mock-normal"}})
    r = client.post("/api/chat", json={"message": "洛必达法则是什么？", "document_ids": [doc_id]}).json()
    check("非流式 /api/chat 正常", r["code"] == 0 and bool(r["data"]["answer"]), str(r["data"].get("answer", ""))[:40])
    check("非流式引用非空", len(r["data"]["citations"]) >= 1, str(r["data"]["citations"]))
    check("非流式引用带 page_no 整数", all(c["page_no"] == db_page(c["chunk_id"]) for c in r["data"]["citations"]))

    print("-" * 60)
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
