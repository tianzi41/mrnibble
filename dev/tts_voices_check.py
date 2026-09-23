"""T-音色发现自检：三级降级（官方接口 → 内置清单 → 探测缓存）+ 能力矩阵裁剪。

用「进程内假端点 + 用户覆盖文件 data/tts_providers.json」模拟一个本地 TTS 提供方：
- `GET /audio/voices` 返回 2 个音色（官方来源）
- `GET /models` 返回模型列表
- `POST /audio/speech` 对 `bad-voice` 返回 400，其余 200 + 真 WAV；并记录收到的
  `voice` 与 `input` 长度（用于验证默认音色与 max_chars 裁剪）

覆盖文件把 `127.0.0.1` 映射到一个测试 provider（8 个内置音色 + max_chars=50），
从而在不访问真实站点的情况下验证：主机识别、内置清单、探测缓存、参数裁剪。

用法::

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe dev/tts_voices_check.py
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".tmp" / ("test-data-voices-%d" % int(time.time()))
APPPORT = 8766

PASS: list[str] = []
FAIL: list[str] = []
RECV: list[dict] = []          # 每次合成请求收到的 {voice, input_len}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{('  | ' + detail) if (detail and not cond) else ''}")


def wav_bytes(seconds: float = 1.0, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(1200 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(rate * seconds))))
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/audio/voices"):
            body = json.dumps({"data": [
                {"id": "va", "name": "Voice A"},
                {"id": "vb", "voice_id": "vb"},
            ]}).encode()
            self._send(200, body)
        elif self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": "tts-x"}, {"id": "chat-y"}]}).encode()
            self._send(200, body)
        else:
            self._send(404, b"{}")

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            payload = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            payload = {}
        RECV.append({"voice": payload.get("voice"), "input_len": len(payload.get("input") or "")})
        if payload.get("voice") == "bad-voice":
            body = json.dumps({"error": {"message": "The voice_id (bad-voice) does not exist.",
                                         "type": "voice_id_invalid"}}).encode()
            self._send(400, body)
            return
        body = wav_bytes()
        self._send(200, body, "audio/wav")

    def log_message(self, *a):
        pass


BUILTIN_TEST_VOICES = [f"tv-{i}" for i in range(1, 9)]


def wait_http(url: str, timeout: float = 90.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=3, trust_env=False).status_code < 500:
                return True
        except Exception:
            time.sleep(0.5)
    return False


async def main() -> int:
    DATA.mkdir(parents=True)

    # 用户覆盖文件：把 127.0.0.1 映射到一个测试 provider（验证主机识别 / 内置清单 / caps）
    override = {
        "testlocal": {
            "match_hosts": ["127.0.0.1"],
            "label": "测试本地",
            "models": {
                "*": {
                    "default_voice": "test-default",
                    "voices": [{"id": f"tv-{i}", "label": f"测试音色 {i}", "lang": "zh"}
                               for i in range(1, 9)],
                    "caps": {"max_chars": 50, "formats": ["mp3", "wav"]},
                },
            },
        },
    }
    (DATA / "tts_providers.json").write_text(json.dumps(override, ensure_ascii=False, indent=2),
                                             encoding="utf-8")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    tts_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    env = {**os.environ, "ZHIBAN_DATA_DIR": str(DATA), "ZHIBAN_PORT": "8766",
           "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    be = subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        check("0.1 后端已就绪",
              wait_http("http://127.0.0.1:8766/api/health"))
        if FAIL:
            return 1
        BASE = "http://127.0.0.1:8766"
        with httpx.Client(trust_env=False, timeout=60) as c:
            c.put(f"{BASE}/api/settings", json={
                "tts": {"mode": "cloud", "enabled": True,
                        "base_url": f"http://127.0.0.1:{tts_port}",
                        "model": "stepaudio-2.5-tts", "voice": ""}})

            # ── 1. 音色三级合并 ──
            r = c.get(f"{BASE}/api/tts/voices", timeout=30).json()["data"]
            by_id = {v["id"]: v for v in r["items"]}
            check("1.1 官方来源的音色已并入（va/vb）",
                  by_id.get("va", {}).get("source") == "api" and by_id.get("vb", {}).get("source") == "api",
                  str(sorted(by_id))[:160])
            check("1.2 内置清单已并入（覆盖文件里的 8 个）",
                  all(f"tv-{i}" in by_id and by_id[f"tv-{i}"]["source"] == "builtin"
                      for i in range(1, 9)),
                  str(sorted(by_id))[:160])
            check("1.3 provider 按 host 识别为 testlocal（而非 generic）",
                  r.get("provider") == "testlocal", str(r.get("provider")))
            check("1.4 默认音色来自覆盖文件（test-default，而非写死 alloy）",
                  r.get("default_voice") == "test-default", str(r.get("default_voice")))

            # ── 2. 模型自动拉取 ──
            m = c.get(f"{BASE}/api/settings/models?target=tts", timeout=30).json()["data"]
            check("2.1 target=tts 可拉取模型列表", "tts-x" in (m.get("models") or []),
                  str(m)[:160])

            # ── 3. 探测（含失败样本）──
            pr = c.post(f"{BASE}/api/tts/voices/probe",
                        json={"voices": ["good-voice", "bad-voice"], "limit": 12},
                        timeout=60).json()["data"]
            by = {x["id"]: x for x in pr["items"]}
            check("3.1 探测：good-voice 可用", by.get("good-voice", {}).get("ok") is True, str(by)[:200])
            check("3.2 探测：bad-voice 不可用且带上游原因",
                  by.get("bad-voice", {}).get("ok") is False
                  and "does not exist" in (by.get("bad-voice", {}).get("error") or ""),
                  str(by.get("bad-voice"))[:200])
            cache = json.loads((DATA / "tts_voices_cache.json").read_text(encoding="utf-8"))
            check("3.3 探测成功者已写进缓存",
                  any(v["id"] == "good-voice" for v in cache.get("voices", [])),
                  str(cache)[:160])

            # ── 4. 探测缓存进入第三级 ──
            r2 = c.get(f"{BASE}/api/tts/voices", timeout=30).json()["data"]
            by2 = {v["id"]: v for v in r2["items"]}
            check("4.1 探测缓存进入第三级来源",
                  by2.get("good-voice", {}).get("source") == "probe",
                  str(sorted(by2))[:160])

            # ── 5. caps：max_chars 裁剪 + 默认音色 ──
            RECV.clear()
            long_text = "字" * 200
            sp = c.post(f"{BASE}/api/tts/speech", json={"text": long_text}, timeout=60)
            check("5.1 超长文本被裁剪到 max_chars（50）",
                  sp.status_code == 200 and RECV and RECV[-1]["input_len"] <= 50,
                  f"input_len={RECV[-1]['input_len'] if RECV else 'n/a'}")
            check("5.2 voice 留空 → 发送 default_voice（test-default）",
                  RECV and RECV[-1]["voice"] == "test-default",
                  f"voice={RECV[-1]['voice'] if RECV else 'n/a'}")
            check("5.3 响应头 X-TTS-Dropped 已回显被截断字段",
                  bool(sp.headers.get("x-tts-dropped")),
                  str(dict(sp.headers))[:200])

            # ── 6. 格式不在 caps → 丢弃并回显 ──
            RECV.clear()
            sp2 = c.post(f"{BASE}/api/tts/speech", json={"text": "你好", "format": "opus"},
                         timeout=60)
            check("6.1 不支持的 response_format 被丢弃并回显",
                  sp2.status_code == 200 and "opus" in (sp2.headers.get("x-tts-dropped") or ""),
                  str(dict(sp2.headers))[:200])
        # 数据目录里的覆盖文件 / 缓存随临时目录一起清理
    finally:
        try:
            be.terminate()
            be.wait(timeout=10)
        except Exception:
            try:
                be.kill()
            except Exception:
                pass
        srv.shutdown()

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
