"""TTS 端点/Key 回退自检（进程内直测，不依赖起后端）。

验证 settings_service.SettingsService.tts_effective() 的回退逻辑，并真正发一次
请求证明「同主机沿用 Key / 异主机不发 Key」的行为。

- 数据目录用独立临时目录（ZHIBAN_DATA_DIR），**不污染 data/**；
- 用标准库起进程内 ThreadingHTTPServer 当 mock 语音端点，记录收到的
  Authorization 头，断言同主机带 Key、异主机不带 Key（防密钥外泄）。

运行::

    .venv/Scripts/python.exe dev/tts_fallback_check.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 必须在导入 backend 之前设置隔离数据目录（config 在首次访问时定稿）。
_TMP = tempfile.mkdtemp(prefix="zhiban_tts_")
os.environ["ZHIBAN_DATA_DIR"] = _TMP

sys.path.insert(0, str(ROOT / "src"))

from backend.db.connection import get_db  # noqa: E402
from backend.services.settings_service import SettingsService  # noqa: E402
from backend.services.tts import TTSService  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  -> {detail}")


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        auth = self.headers.get("Authorization")
        server = self.server  # type: ignore[attr-defined]
        server.last_auth = auth  # type: ignore[attr-defined]
        server.last_path = self.path  # type: ignore[attr-defined]
        server.hits += 1  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(b"fake-audio")))
        self.end_headers()
        self.wfile.write(b"fake-audio")

    def log_message(self, *args: object) -> None:  # 静默
        pass


class _Handler2(BaseHTTPRequestHandler):
    """按路径返回不同状态码：/audio/speech=200、/v1/audio/speech=404、/boom/audio/speech=401。"""
    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        server = self.server  # type: ignore[attr-defined]
        server.last_auth = self.headers.get("Authorization")  # type: ignore[attr-defined]
        server.last_path = self.path  # type: ignore[attr-defined]
        server.hits += 1  # type: ignore[attr-defined]
        if self.path.endswith("/boom/audio/speech"):
            code = 401
        elif self.path.endswith("/v1/audio/speech"):
            code = 404
        else:
            code = 200
        self.send_response(code)
        self.send_header("Content-Type", "audio/mpeg")
        if code == 200:
            self.send_header("Content-Length", str(len(b"fake-audio")))
            self.end_headers()
            self.wfile.write(b"fake-audio")
        else:
            self.end_headers()

    def log_message(self, *args: object) -> None:  # 静默
        pass


class _Handler3(BaseHTTPRequestHandler):
    """按请求体 voice 字段返回不同结果（测试 11~14：上游报错暴露 + 音色提示）。

    - voice="alloy"           → 400 + JSON（voice_id 报错，模拟 StepFun）
    - voice="cixingnansheng"  → 200 + 一小段音频字节
    - voice="plaintext"       → 400 + 非 JSON 纯文本
    - 其它（如 "othermodel"） → 400 + JSON（无 voice 字样）
    """
    def do_POST(self) -> None:  # noqa: N802
        import json as _json

        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) or b""
        server = self.server  # type: ignore[attr-defined]
        server.last_path = self.path  # type: ignore[attr-defined]
        server.hits += 1  # type: ignore[attr-defined]
        try:
            data = _json.loads(body.decode("utf-8", "replace"))
            voice = data.get("voice", "")
        except Exception:
            voice = ""
        if voice == "alloy":
            code = 400
            payload = (b'{"error":{"message":"The voice_id (alloy) does not exist '
                       b'or you do not have access to it.","type":"voice_id_invalid"}}')
            ctype = "application/json"
        elif voice == "cixingnansheng":
            code = 200
            payload = b"fake-audio"
            ctype = "audio/mpeg"
        elif voice == "plaintext":
            code = 400
            payload = b"bad voice param"
            ctype = "text/plain"
        else:
            code = 400
            payload = b'{"message":"model not found"}'
            ctype = "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # 静默
        pass


def main() -> int:
    get_db().migrate()
    sv = SettingsService.get_instance()

    def setv(key: str, value: str, secret: bool = False) -> None:
        sv.set_value(key, value, secret)

    def eff() -> tuple[str, str, str]:
        return sv.tts_effective()

    # 先清空相关键，保证互不影响。
    for k, sec in [("llm.base_url", False), ("llm.model", False), ("llm.api_key", True),
                   ("tts.base_url", False), ("tts.model", False), ("tts.api_key", True),
                   ("tts.mode", False)]:
        setv(k, "", sec)

    # ── 纯逻辑断言（不联网） ──────────────────────────────
    # 1) 端点沿用
    setv("llm.base_url", "http://127.0.0.1:11999/v1")
    setv("tts.base_url", "")
    check("1 端点沿用对话模型", eff()[0] == "http://127.0.0.1:11999/v1", eff()[0])

    # 2) 同主机 Key 沿用
    setv("llm.api_key", "sk-llm-test", True)
    setv("tts.api_key", "")
    check("2 同主机 Key 沿用", eff()[2] == "sk-llm-test", eff()[2])

    # 3) 异主机 Key 不沿用（安全关键）
    setv("tts.base_url", "http://localhost:11999/v1")  # netloc 与 127.0.0.1 不同
    setv("tts.api_key", "")
    check("3 异主机 Key 不沿用（不发 Key）", eff()[2] == "", repr(eff()[2]))

    # 4) 显式 Key 优先
    setv("tts.api_key", "sk-tts-test", True)
    check("4 显式 Key 优先", eff()[2] == "sk-tts-test", eff()[2])

    # 5) 模型名不回退
    setv("tts.model", "")
    setv("llm.model", "deepseek-chat")
    check("5 模型名不回退", eff()[1] == "", repr(eff()[1]))

    # ── 真发请求证明行为 ─────────────────────────────────
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.last_auth = None
    server.last_path = None
    server.hits = 0
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        setv("tts.mode", "cloud")
        # 场景 A（同主机）：llm 与 tts 端点同指向 mock
        setv("llm.base_url", f"http://127.0.0.1:{port}/v1")
        setv("tts.base_url", "")  # 留空 → 沿用 llm
        setv("llm.api_key", "sk-llm-test", True)
        setv("tts.api_key", "")
        setv("tts.model", "tts-1")
        server.hits = 0
        server.last_auth = None
        TTSService.get_instance().speech("你好")
        check("6A 同主机请求带 Authorization=Bearer sk-llm-test",
              server.last_auth == "Bearer sk-llm-test", str(server.last_auth))
        check("6A 请求打到 /audio/speech", server.last_path == "/v1/audio/speech", str(server.last_path))

        # 场景 B（异主机）：tts 指向 localhost（同端口但不同 netloc）
        setv("tts.base_url", f"http://localhost:{port}/v1")
        setv("tts.api_key", "")
        server.hits = 0
        server.last_auth = None
        TTSService.get_instance().speech("你好")
        check("6B 异主机请求不带 Authorization（防密钥外泄）",
              server.last_auth is None, str(server.last_auth))
    finally:
        server.shutdown()

    # ── 7~10：配置/回退错误提示（独立于上面 6A/6B 的 mock，按路径返回状态码）──
    server2 = ThreadingHTTPServer(("127.0.0.1", 0), _Handler2)
    server2.last_auth = None
    server2.last_path = None
    server2.hits = 0
    port2 = server2.server_address[1]
    t2 = threading.Thread(target=server2.serve_forever, daemon=True)
    t2.start()

    def _tts_msg() -> str:
        """调用 _test_tts，统一返回「给用户看的那条 message」（成功/失败都收口到这里）。"""
        try:
            code, warning = sv._test_tts()
            return warning or code
        except Exception as e:  # AppError 等非 2xx 落到这里
            # 失败分支：message 与 detail 都带上（HTTP 状态常在 detail，鉴权失败常在 message）。
            return (getattr(e, "message", "") + " | " + (getattr(e, "detail", "") or "")).strip(" |")

    try:
        # 7) 云端未配置：mode=cloud 但 base_url/model 都空且 llm.base_url 也空
        setv("llm.base_url", "")
        setv("tts.base_url", "")
        setv("tts.model", "")
        setv("tts.mode", "cloud")
        try:
            sv._test_tts()
            check("7 云端未配置报错含「端点」", False, "未抛异常")
        except Exception as e:
            check("7 云端未配置报错含「端点」",
                  "端点" in getattr(e, "message", str(e)), getattr(e, "message", str(e)))

        # 8) 端点返回 404 → message 含 404 且含该端点 host
        setv("llm.base_url", "")
        setv("tts.base_url", f"http://127.0.0.1:{port2}/v1")
        setv("tts.model", "tts-1")
        setv("tts.api_key", "")
        msg8 = _tts_msg()
        check("8 端点 404 → message 含 404", "404" in msg8, msg8)
        check("8 端点 404 → message 含 host", f"127.0.0.1:{port2}" in msg8, msg8)

        # 9) 端点返回 401 → message 含 401
        setv("tts.base_url", f"http://127.0.0.1:{port2}/boom")
        setv("tts.model", "tts-1")
        msg9 = _tts_msg()
        check("9 端点 401 → message 含 401", "401" in msg9, msg9)

        # 10) tts.base_url 留空、沿用 llm.base_url → message 含「沿用」
        # 用根路径（/audio/speech 返回 200），避免与 8 的 /v1 404 路径冲突。
        setv("tts.base_url", "")
        setv("llm.base_url", f"http://127.0.0.1:{port2}")
        setv("tts.model", "tts-1")
        msg10 = _tts_msg()
        check("10 留空沿用 llm → message 含「沿用」", "沿用" in msg10, msg10)
    finally:
        server2.shutdown()

    # ── 11~14：上游报错暴露 + 音色针对性提示（voice 字段驱动 mock）──
    server3 = ThreadingHTTPServer(("127.0.0.1", 0), _Handler3)
    server3.last_path = None
    server3.hits = 0
    port3 = server3.server_address[1]
    t3 = threading.Thread(target=server3.serve_forever, daemon=True)
    t3.start()
    try:
        setv("llm.base_url", "")
        setv("tts.base_url", f"http://127.0.0.1:{port3}/v1")
        setv("tts.model", "tts-1")
        setv("tts.api_key", "")

        # 11) voice=alloy → 400 + voice_id 报错 → 必须抛 AppError（不再返回成功串），
        #     detail 含 voice_id/does not exist，且含「音色」针对性提示。
        setv("tts.voice", "alloy")
        try:
            sv._test_tts()
            check("11 alloy 400 → 抛 AppError（非成功串）", False, "未抛异常（返回了成功串）")
        except Exception as e:
            detail11 = getattr(e, "detail", "") or getattr(e, "message", str(e))
            check("11 alloy 400 → 抛 AppError（非成功串）", True)
            check("11 detail 含 voice_id 或 does not exist",
                  ("voice_id" in detail11) or ("does not exist" in detail11), detail11)
            check("11 detail 含「音色」针对性提示", "音色" in detail11, detail11)

        # 12) voice=cixingnansheng → 200 + 音频 → message 含 HTTP 200 与假端点 host（回归保护）。
        setv("tts.voice", "cixingnansheng")
        msg12 = _tts_msg()
        check("12 cixingnansheng 200 → message 含 HTTP 200", "HTTP 200" in msg12, msg12)
        check("12 message 含假端点 host", f"127.0.0.1:{port3}" in msg12, msg12)

        # 13) 非 JSON 纯文本 400 → 不崩溃，detail 含该文本片段。
        setv("tts.voice", "plaintext")
        try:
            sv._test_tts()
            check("13 纯文本 400 → 抛 AppError", False, "未抛异常")
        except Exception as e:
            detail13 = getattr(e, "detail", "") or getattr(e, "message", str(e))
            check("13 纯文本 400 → 不崩溃且 detail 含 'bad voice param'",
                  "bad voice param" in detail13, detail13)

        # 14) 无 voice 字样 400 → detail 不含「音色」提示（证明没乱猜）。
        setv("tts.voice", "othermodel")
        try:
            sv._test_tts()
            check("14 无 voice 字样 → 抛 AppError", False, "未抛异常")
        except Exception as e:
            detail14 = getattr(e, "detail", "") or getattr(e, "message", str(e))
            check("14 无 voice 字样 → detail 不含「音色」提示", "音色" not in detail14, detail14)
    finally:
        server3.shutdown()

    print("-" * 50)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
