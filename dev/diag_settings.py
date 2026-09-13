"""诊断：设置保存后 GET /api/settings 是否回显（复现「配置了却显示未配置模型」）。

场景：
  A. 同时填 base_url + 模型名 + Key   → 期望 badge 可点亮
  B. 只填 base_url + Key（模型名留空）→ 复现用户现象
  C. 只填模型名（base_url 留空）
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
DATA = ROOT / ".tmp" / "diag-settings"
BASE = "http://127.0.0.1:8760"
C = dict(trust_env=False, timeout=20.0)


def start_backend() -> subprocess.Popen:
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)
    env = {
        **os.environ,
        "ZHIBAN_DATA_DIR": str(DATA),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.Popen(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "backend.main"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        try:
            if httpx.get(f"{BASE}/api/health", **C).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("后端未能在 30 秒内启动")


def badge_ok(cfg: dict) -> bool:
    """复刻前端 main.js:39 的判定。"""
    return bool(cfg["llm"]["base_url"] and cfg["llm"]["model"])


def case(name: str, patch: dict) -> None:
    r = httpx.put(f"{BASE}/api/settings", json=patch, **C).json()
    print(f"\n── {name}")
    print("   PUT updated =", r["data"]["updated"])
    llm = httpx.get(f"{BASE}/api/settings", **C).json()["data"]["llm"]
    print("   GET llm     =", {k: llm.get(k) for k in ("base_url", "model", "api_key_set")})
    print("   右上角判定  =", "✅ 显示模型名" if badge_ok({"llm": llm}) else "❌ 未配置模型")


def main() -> int:
    proc = start_backend()
    try:
        case(
            "A. base_url + 模型名 + Key（完整）",
            {"llm": {"base_url": "https://api.siliconflow.cn/v1",
                     "model": "Qwen/Qwen2.5-7B-Instruct",
                     "api_key": "sk-test-1234567890abcdef"}},
        )
        case(
            "B. 只填 base_url + Key（模型名留空）",
            {"llm": {"base_url": "https://api.siliconflow.cn/v1",
                     "model": "",
                     "api_key": "sk-test-1234567890abcdef"}},
        )
        case(
            "C. 只填模型名（base_url 留空）",
            {"llm": {"base_url": "", "model": "deepseek-chat"}},
        )
        case(
            "D. 仅点「测试连接」自动保存（saveIfChanged 只发 base_url+model）",
            {"llm": {"base_url": "https://api.deepseek.com/v1",
                     "model": "deepseek-chat"}},
        )

        # E. 新增校验：模型名为空时必须明确报「尚未填写模型名」，不能再报连接成功。
        httpx.put(f"{BASE}/api/settings", json={
            "llm": {"base_url": "https://api.deepseek.com/v1", "model": ""}}, **C)
        r = httpx.post(f"{BASE}/api/settings/test", json={"target": "llm"}, **C).json()
        print("\n── E. 模型名为空时点「测试连接」")
        print("   code/message =", r["code"], "/", r["message"])
        print("   判定         =", "✅ 已拦下（提示填模型名）" if r["code"] == 2000 and "模型名" in r["message"]
              else "❌ 仍然静默报告成功")

        # F. 模型名不在端点可用列表时，应给出 warning（mock 端点无 /models 数据，故这里只验证不崩）
        r = httpx.post(f"{BASE}/api/settings/test", json={"target": "embed"}, **C).json()
        print("\n── F. 嵌入端点测试（base_url 留空 → 沿用对话模型地址）")
        print("   code/message =", r["code"], "/", r["message"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
