"""复现「上传文件后提问，AI 说没收到文件内容」——查清检索为何为空。

用用户真实上传的 学习好文.txt（.bat 编码那篇），依次试几种问法，
打印每个查询的命中数与来源分数，判断是哪条通道失守。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".tmp" / "dbg-material"
BACKEND = "http://127.0.0.1:8760"
MOCK = "http://127.0.0.1:8761"
C = dict(trust_env=False, timeout=60.0)

SRC_TXT = ROOT / "dist4" / "知伴" / "data" / "files" / "446a930ad67141568eb330d030ea71bd.txt"

QUERIES = [
    "开始学习",
    "看下学习好文.txt的具体内容，然后安排学习",
    "学习好文.txt",
    "这份文件讲了什么？帮我安排学习",
    "帮我总结一下这篇文档",
    "这篇文档的重点是什么",
    "文档里说了什么内容",
    "为什么 .bat 里写中文会闪退",
    "量子纠缠的三体拓扑结构是什么？",
]


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)
    env = {**os.environ, "ZHIBAN_DATA_DIR": str(DATA),
           "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    mock = subprocess.Popen([py, str(ROOT / "dev" / "mock_llm.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    be = subprocess.Popen([py, "-m", "backend.main"], cwd=str(ROOT), env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for url in (MOCK + "/health", BACKEND + "/api/health"):
            for _ in range(80):
                try:
                    if httpx.get(url, **C).status_code < 500:
                        break
                except Exception:
                    time.sleep(0.4)
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {
            "base_url": MOCK + "/v1", "model": "mock-normal",
            "api_key": "sk-test-1234567890abcdef"}}, **C)

        data = SRC_TXT.read_bytes() if SRC_TXT.exists() else "洛必达法则测试文档。".encode()
        up = httpx.post(f"{BACKEND}/api/documents/upload",
                        files={"files": ("学习好文.txt", data, "text/plain")}, **C).json()
        did = up["data"]["documents"][0]["id"]
        for _ in range(40):
            d = httpx.get(f"{BACKEND}/api/documents/{did}", **C).json()["data"]["document"]
            if d["status"] in ("ready", "failed"):
                break
            time.sleep(0.4)
        print(f"文档: {d['title']} status={d['status']} pages={d['page_count']} id={did}\n")

        for q in QUERIES:
            r = httpx.post(f"{BACKEND}/api/retrieve",
                           json={"query": q, "document_ids": [did]}, **C).json()
            hits = (r.get("data") or {}).get("hits") or []
            print(f"命中 {len(hits):>2} | fts={[round(h.get('fts_score') or 0, 3) for h in hits[:3]]} "
                  f"vec={[round(h.get('vec_score') or 0, 3) for h in hits[:3]]}")
            print(f"        Q: {q}")
            if hits:
                print(f"        首条: {(hits[0].get('snippet') or '')[:60]!r} section={hits[0].get('section')!r}")

        # ── 问答链路：材料是否真的进了提示词 ──────────────
        print("\n" + "=" * 70)
        print("问答验证（mock-echo-context 会把注入的上下文原样回显）")
        httpx.put(f"{BACKEND}/api/settings",
                  json={"llm": {"model": "mock-echo-context"}}, **C)
        conv = httpx.post(f"{BACKEND}/api/conversations",
                          json={"mode": "normal", "document_ids": [did]}, **C).json()["data"]

        def ask(q: str) -> dict:
            return httpx.post(f"{BACKEND}/api/chat", json={
                "conversation_id": conv["id"], "message": q}, **C).json()

        for label, q, expect_material in (
            ("用户原话", "看下学习好文.txt的具体内容，然后安排学习", True),
            ("文档级问法", "帮我总结一下这篇文档", True),
            ("课程开场", "开始学习", True),
            ("材料外问题（红线）", "量子纠缠的三体拓扑结构是什么？", False),
        ):
            r = ask(q)
            d = r.get("data") or {}
            ans = d.get("answer") or r.get("message") or ""
            got_material = "MOCK-ECHO" in ans and "[材料1]" in ans
            print(f"\n── {label}：{q}")
            print(f"   retrieved={d.get('retrieved')} citations={len(d.get('citations') or [])} "
                  f"grounded={d.get('grounded')}")
            print(f"   材料进提示词 = {got_material}（期望 {expect_material}）"
                  f"  {'✅' if got_material == expect_material else '❌'}")
            if "【材料概览】" in ans:
                head = ans.split("【材料概览】", 1)[1][:150].replace("\n", " / ")
                print(f"   概览片段: 【材料概览】{head}")
            if not got_material and "材料中未提及" in ans:
                print("   走的是「材料中未提及」确定性回复（红线行为）")
    finally:
        for p in (be, mock):
            p.terminate()
            try:
                p.wait(timeout=8)
            except Exception:
                p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
