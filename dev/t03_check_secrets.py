"""T03 自检：密钥安全（一票否决项）。

验证：
1. 保存假 Key 后，DB 中存的是密文（非明文，Fernet 形态）；
2. ``GET /api/settings`` 只返回掩码，响应体全文不含明文；
3. ``data/logs/`` 全目录 grep 假 Key 明文，命中数为 0；
4. 未配置/错误端点 ``POST /api/settings/test`` 返回 ``code=2003``。

运行前先启动服务（默认 http://127.0.0.1:8760）：
    PYTHONPATH=src .venv/Scripts/python.exe dev/t03_check_secrets.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FAKE_KEY = "sk-test-1234567890abcdef"
BASE = "http://127.0.0.1:8760"
DB_PATH = ROOT / "data" / "mrnibble.db"
LOG_DIR = ROOT / "data" / "logs"

PASSED = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    """断言并记录。"""
    global PASSED
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {extra}" if extra else ""))
    if not condition:
        raise SystemExit(f"断言失败：{name} {extra}")
    PASSED += 1


def http(method: str, path: str, body: dict | None = None) -> dict:
    """极简 HTTP 调用（仅用标准库，避免额外依赖）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    # 1) 保存假 Key，并把 llm 端点指向一个不可达地址（供测试连接失败）。
    payload = {
        "llm": {
            "base_url": "http://127.0.0.1:9/v1",
            "model": "test-model",
            "api_key": FAKE_KEY,
        }
    }
    resp = http("PUT", "/api/settings", payload)
    check("PUT /api/settings 成功", resp.get("code") == 0, str(resp.get("code")))
    check("更新列表包含 llm.api_key", "llm.api_key" in resp["data"].get("updated", []))

    # 2) DB 中应为密文。
    conn = sqlite3.connect(str(DB_PATH))
    try:
        stored = conn.execute(
            "SELECT value, is_secret FROM settings WHERE key='llm.api_key'"
        ).fetchone()
    finally:
        conn.close()
    check("DB 中存在 llm.api_key", stored is not None)
    stored_value, is_secret = stored
    check("is_secret 标记为 1", int(is_secret) == 1, str(is_secret))
    check("DB 存的是密文（不等于明文）", stored_value != FAKE_KEY)
    check("DB 值为 Fernet 形态(gAAAAA...)", str(stored_value).startswith("gAAAAA"))
    check("DB 值不含明文", FAKE_KEY not in str(stored_value))

    # 3) GET /api/settings 只回掩码，响应体全文不含明文。
    public = http("GET", "/api/settings")
    raw_body = json.dumps(public, ensure_ascii=False)
    llm = public["data"]["llm"]
    check("api_key_set 为 true", llm.get("api_key_set") is True)
    check("api_key_masked 为掩码", "****" in llm.get("api_key_masked", ""), llm.get("api_key_masked"))
    check("GET /api/settings 响应体不含明文 Key", FAKE_KEY not in raw_body)
    check("GET /api/settings 不含 api_key 明文字段", "api_key" not in llm)

    # 4) 日志 grep 假 Key，命中必须为 0。
    hits = 0
    hit_files: list[str] = []
    for log_file in LOG_DIR.glob("*.log*"):
        text = log_file.read_text(encoding="utf-8", errors="ignore")
        if FAKE_KEY in text:
            hits += text.count(FAKE_KEY)
            hit_files.append(log_file.name)
    check("data/logs 全目录 grep 假 Key 命中数为 0", hits == 0, f"命中 {hits} 次 {hit_files}")

    # 5) 连接测试：错误端点应返回 code=2003。
    try:
        test_resp = http("POST", "/api/settings/test", {"target": "llm"})
        code = test_resp.get("code")
    except urllib.error.HTTPError as exc:  # 非 2xx 时读取封套
        code = json.loads(exc.read().decode("utf-8")).get("code")
    check("错误端点连接测试返回 code=2003", code == 2003, f"code={code}")

    print("-" * 60)
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
