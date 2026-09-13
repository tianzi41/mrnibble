"""T09 自检：五类资料生成（速查表/笔记/导图/测验/闪卡）。

前置：先启动 mock LLM(8761) 与主服务(8760)，并已上传样例文档。

判据（R-C01~C05）：
1. cheatsheet ≥5 条目；
2. notes ≥4 规定小节（核心概念/口诀/易错点/章节框架）；
3. mindmap 层级 MD 根唯一、可被 markmap 渲染；
4. quiz 每题四要素齐（stem/options/answer_index/explanation）；
5. flashcard 每张 question/answer 齐并**入 flashcards 表**。

运行：
    PYTHONPATH=src .venv/Scripts/python.exe dev/t09_check_generation.py
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8760"
MOCK_BASE = "http://127.0.0.1:8761/v1"
PASSED = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    """断言并记录。"""
    global PASSED
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {extra}" if extra else ""))
    if not condition:
        raise SystemExit(f"断言失败：{name} {extra}")
    PASSED += 1


def find_doc(client: httpx.Client, needle: str) -> str:
    """找一份就绪文档。"""
    items = client.get("/api/documents").json()["data"]["items"]
    for doc in items:
        if needle in (doc.get("title") or "") and doc.get("status") == "ready":
            return doc["id"]
    for doc in items:
        if doc.get("status") == "ready":
            return doc["id"]
    raise SystemExit("资料库为空：请先运行 t06_check_chat.py 上传样例文档")


def generate(client: httpx.Client, gen_type: str, doc_id: str, count: int = 5) -> dict:
    """创建并等待一个生成任务完成。"""
    client.put("/api/settings", json={"llm": {"model": f"mock-{gen_type}"}})
    created = client.post(
        "/api/generations",
        json={"type": gen_type, "document_ids": [doc_id], "params": {"count": count}},
    ).json()
    check(f"{gen_type} 创建返回 running", created["data"]["status"] == "running", str(created["data"]))
    gen_id = created["data"]["generation_id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        record = client.get(f"/api/generations/{gen_id}").json()["data"]["generation"]
        if record["status"] in ("ready", "failed"):
            if record["status"] == "failed":
                raise SystemExit(f"{gen_type} 生成失败：{record.get('error')}")
            return record
        time.sleep(0.2)
    raise SystemExit(f"{gen_type} 生成超时")


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=60.0)
    client.put("/api/settings", json={"llm": {"base_url": MOCK_BASE}})
    doc_id = find_doc(client, "微积分")

    # 1) cheatsheet
    gen = generate(client, "cheatsheet", doc_id)
    items = gen["content_json"]["items"]
    check("cheatsheet ≥5 条目", len(items) >= 5, str(len(items)))
    check("cheatsheet 条目含 point/detail", all("point" in i and "detail" in i for i in items))
    check("cheatsheet 有展示 md", bool(gen.get("content_md")))

    # 2) notes
    gen = generate(client, "notes", doc_id)
    sections = gen["content_json"]["sections"]
    check("notes ≥4 小节", len(sections) >= 4, str(len(sections)))
    headings = {s["heading"] for s in sections}
    expected = {"核心概念", "口诀", "易错点", "章节框架"}
    check("notes 含规定小节", expected.issubset(headings), str(headings))

    # 3) mindmap
    gen = generate(client, "mindmap", doc_id)
    md = gen.get("content_md") or ""
    root_lines = [ln for ln in md.splitlines() if ln.startswith("# ")]
    check("mindmap 根唯一", len(root_lines) == 1, str(root_lines))
    check("mindmap 含子级", any(ln.startswith("## ") for ln in md.splitlines()))
    check("mindmap content_json.root 存在", isinstance(gen["content_json"].get("root"), dict))

    # 4) quiz
    gen = generate(client, "quiz", doc_id)
    qitems = gen["content_json"]["items"]
    check("quiz 有题目", len(qitems) >= 1, str(len(qitems)))
    ok_four = all(
        q.get("stem") and len(q.get("options", [])) >= 2 and isinstance(q.get("answer_index"), int) and q.get("explanation") is not None
        for q in qitems
    )
    check("quiz 每题四要素齐", ok_four, str(qitems[0] if qitems else {}))
    check("quiz options 为四选项", all(len(q.get("options", [])) == 4 for q in qitems))

    # 5) flashcard（并落库）
    gen = generate(client, "flashcard", doc_id)
    fitems = gen["content_json"]["items"]
    check("flashcard 有卡片", len(fitems) >= 1, str(len(fitems)))
    check("flashcard 每张问答齐", all(i.get("question") and i.get("answer") for i in fitems))
    stored = client.get("/api/flashcards", params={"generation_id": gen["id"]}).json()["data"]["items"]
    check("flashcard 已入 flashcards 表", len(stored) == len(fitems), f"{len(stored)} vs {len(fitems)}")

    # 复习一次（T13 路径）
    reviewed = client.post(
        f"/api/flashcards/{stored[0]['id']}/review", json={"remembered": False}
    ).json()["data"]["flashcard"]
    check("复习更新 sr_state", reviewed["sr_state"].get("lapses", 0) >= 1, str(reviewed["sr_state"]))

    # 列表过滤
    listing = client.get("/api/generations", params={"type": "quiz"}).json()["data"]["items"]
    check("生成列表按类型过滤", all(g["type"] == "quiz" for g in listing) and len(listing) >= 1)

    print("-" * 60)
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
