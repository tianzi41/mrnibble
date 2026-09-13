"""定点排查：材料外问题为何仍召回命中（F 组回归）。

复现步骤：起 mock + backend → 上传一份「洛必达法则」文档 → 用完全无关的问题检索，
打印每个命中的 fts_raw / vec_raw 与来源通道，判断是哪条通道漏进来的。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".tmp" / "dbg-f"
BACKEND = "http://127.0.0.1:8760"
MOCK = "http://127.0.0.1:8761"
C = dict(trust_env=False, timeout=30.0)

DOC = (
    "## 3.2 洛必达法则\n\n"
    "洛必达法则是求未定式极限的重要方法，适用于 0/0 型或 ∞/∞ 型未定式。\n"
    "使用前必须先验证类型，否则会误用。\n"
)


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
        for url, name in ((MOCK + "/health", "mock"), (BACKEND + "/api/health", "backend")):
            for _ in range(80):
                try:
                    if httpx.get(url, **C).status_code < 500:
                        break
                except Exception:
                    time.sleep(0.4)
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {
            "base_url": MOCK + "/v1", "model": "mock-normal",
            "api_key": "sk-test-1234567890abcdef"}}, **C)

        up = httpx.post(f"{BACKEND}/api/documents/upload",
                        files={"files": ("t.md", io.BytesIO(DOC.encode()), "text/markdown")}, **C).json()
        did = up["data"]["documents"][0]["id"]
        for _ in range(40):
            d = httpx.get(f"{BACKEND}/api/documents/{did}", **C).json()["data"]["document"]
            if d["status"] in ("ready", "failed"):
                break
            time.sleep(0.4)
        print("文档状态:", d["status"])

        # 嵌入通道现状
        print("嵌入 provider:", httpx.get(f"{BACKEND}/api/settings", **C).json()["data"].get("embed"))

        for q in ("量子纠缠的三体拓扑结构是什么？", "洛必达法则怎么用？"):
            r = httpx.post(f"{BACKEND}/api/retrieve", json={"query": q}, **C).json()["data"]
            print(f"\n── 查询：{q}")
            print("   命中数:", len(r["hits"]), " degraded:", r.get("degraded"))
            for h in r["hits"][:5]:
                print(f"   · score={h.get('score')} fts={h.get('fts_score')} vec={h.get('vec_score')}")
                print(f"     文本：{(h.get('snippet') or '')[:60]}")
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
