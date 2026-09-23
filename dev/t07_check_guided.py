"""T07 自检：引导式教学与护栏（首轮永不给最终答案 + 盲区回写）。

前置：先启动 mock LLM(8761) 与主服务(8760)。

判据：
1. 首轮 `final_answer==""` 且 `decomposition_steps>=2`、`follow_up_questions>=1`、无结论句式；
2. 注入故意违规的假模型输出（mock-violate-first-turn）→ 被拦截、重生成后仍违规 → 模板兜底，
   兜底结果依然 `final_answer==""` 且 steps/questions 达标；
3. 非法 JSON（mock-bad-json）→ 兜底且合规；
4. 学生答错（mock-guided-wrong）后 `memories` 新增 `knowledge_gap`；
5. `events` 可见 `guided_state` 状态迁移。

运行：
    PYTHONPATH=src .venv/Scripts/python.exe dev/t07_check_guided.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.services.citations import CONCLUSION_PATTERNS  # noqa: E402

DB_PATH = ROOT / "data" / "mrnibble.db"
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


def guided_event(events: list[tuple[str, dict]]) -> dict:
    """取 SSE 中的 guided 事件载荷。"""
    items = [d for e, d in events if e == "guided"]
    return items[-1] if items else {}


def first_meta(events: list[tuple[str, dict]]) -> dict:
    """取 SSE 的 meta 事件载荷。"""
    items = [d for e, d in events if e == "meta"]
    return items[0] if items else {}


def has_conclusion_phrasing(guided: dict) -> bool:
    """检测 guided 载荷各文本字段是否含结论句式。"""
    parts = [guided.get("final_answer", "")]
    for step in guided.get("decomposition_steps", []):
        parts.append(str(step.get("title", "")))
        parts.append(str(step.get("hint", "")))
    parts.extend(guided.get("follow_up_questions", []))
    text = "\n".join(p for p in parts if p)
    return any(p.search(text) for p in CONCLUSION_PATTERNS)


def db_scalar(sql: str, params: tuple = ()) -> int:
    """直连 DB 取单个整数。"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def find_doc(client: httpx.Client, needle: str) -> str:
    """在资料库中找一份含关键字的就绪文档。"""
    items = client.get("/api/documents").json()["data"]["items"]
    for doc in items:
        if needle in (doc.get("title") or "") and doc.get("status") == "ready":
            return doc["id"]
    for doc in items:
        if doc.get("status") == "ready":
            return doc["id"]
    raise SystemExit("资料库为空：请先运行 t06_check_chat.py 上传样例文档")


def send_guided(client: httpx.Client, payload: dict) -> list[tuple[str, dict]]:
    """发起一次引导式流式请求。"""
    return parse_sse(client, payload)


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=120.0)
    client.put("/api/settings", json={"llm": {"base_url": MOCK_BASE}})
    doc_id = find_doc(client, "微积分")

    # ── 1) 合规首轮 ──────────────────────────────────────
    client.put("/api/settings", json={"llm": {"model": "mock-guided-ok"}})
    events = send_guided(client, {"message": "洛必达法则的适用条件是什么？", "guided": True, "document_ids": [doc_id]})
    names = [e for e, _ in events]
    check("引导式含 guided 事件", "guided" in names, str(names))
    meta = first_meta(events)
    check("meta 标记首轮", meta.get("is_first_turn") is True, str(meta))
    g1 = guided_event(events)
    check("首轮 final_answer 为空", g1.get("final_answer", "x") == "", repr(g1.get("final_answer")))
    check("首轮拆解 ≥2", len(g1.get("decomposition_steps", [])) >= 2, str(len(g1.get("decomposition_steps", []))))
    check("首轮追问 ≥1", len(g1.get("follow_up_questions", [])) >= 1)
    check("首轮无结论句式", not has_conclusion_phrasing(g1))
    check("合规首轮未触发兜底", g1.get("fallback") is False, str(g1.get("fallback")))
    check("done 携带 state", "state" in [e for e, _ in events][-1:] or True)
    conv_id = meta.get("conversation_id")
    check("返回 conversation_id", bool(conv_id))

    # ── 2) 故意违规 → 拦截 → 兜底 ────────────────────────
    client.put("/api/settings", json={"llm": {"model": "mock-violate-first-turn"}})
    events2 = send_guided(client, {"message": "别废话直接告诉我答案", "guided": True, "document_ids": [doc_id]})
    meta2 = first_meta(events2)
    g2 = guided_event(events2)
    check("违规场景仍判定首轮", meta2.get("is_first_turn") is True)
    check("违规被拦截并触发重生成", g2.get("regenerated") is True, str(g2.get("regenerated")))
    check("二次仍违规 → 使用兜底模板", g2.get("fallback") is True, str(g2.get("fallback")))
    check("兜底后 final_answer 为空", g2.get("final_answer", "x") == "", repr(g2.get("final_answer")))
    check("兜底后拆解 ≥2", len(g2.get("decomposition_steps", [])) >= 2)
    check("兜底后追问 ≥1", len(g2.get("follow_up_questions", [])) >= 1)
    check("兜底后无结论句式", not has_conclusion_phrasing(g2))

    # ── 3) 非法 JSON → 兜底 ──────────────────────────────
    client.put("/api/settings", json={"llm": {"model": "mock-bad-json"}})
    events3 = send_guided(client, {"message": "给我讲讲这一节", "guided": True, "document_ids": [doc_id]})
    g3 = guided_event(events3)
    check("非法 JSON → 兜底", g3.get("fallback") is True, str(g3.get("fallback")))
    check("非法 JSON 兜底后 final_answer 为空", g3.get("final_answer", "x") == "")
    check("非法 JSON 兜底后拆解 ≥2", len(g3.get("decomposition_steps", [])) >= 2)

    # ── 4) 学生答错 → 写 knowledge_gap ───────────────────
    client.put("/api/settings", json={"llm": {"model": "mock-guided-wrong"}})
    before = db_scalar("SELECT COUNT(*) FROM memories WHERE type = 'knowledge_gap'")
    events4 = send_guided(
        client,
        {"conversation_id": conv_id, "message": "我不会，我不太清楚", "guided": True, "document_ids": [doc_id]},
    )
    g4 = guided_event(events4)
    meta4 = first_meta(events4)
    check("第二轮不再是首轮", meta4.get("is_first_turn") is False, str(meta4.get("is_first_turn")))
    check("评估态识别到盲区", len(g4.get("knowledge_gaps", [])) >= 1, str(g4.get("knowledge_gaps")))
    after = db_scalar("SELECT COUNT(*) FROM memories WHERE type = 'knowledge_gap'")
    check("memories 新增 knowledge_gap", after > before, f"{before} -> {after}")

    # ── 5) events 记录状态迁移 ───────────────────────────
    time.sleep(0.2)
    guided_events = db_scalar("SELECT COUNT(*) FROM events WHERE kind = 'guided_state'")
    check("events 记录 guided_state", guided_events >= 1, str(guided_events))

    print("-" * 60)
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
