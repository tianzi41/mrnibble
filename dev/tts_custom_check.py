"""自定义语音服务适配器（tts.mode=custom）检查。

用一个**假 TTS 服务**（http.server，127.0.0.1:8799）验证：
  ① 未填地址模板 → 4002 且说清去哪填；
  ② GET 模板：{text} 按 URL 编码替换、请求真打到服务上、音频字节原样返回；
  ③ 返回非音频（JSON）→ 4003 且报出真实 Content-Type（不能把 JSON 当音频塞给前端）；
  ④ 端口不通 → 4003 且回显地址模板（省得用户猜是哪儿写错了）。

进程内跑，不依赖后端服务；用独立临时 ZHIBAN_DATA_DIR。

用法::
    .venv/Scripts/python.exe dev/tts_custom_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TMP = ROOT / ".tmp" / ("test-data-ttscustom-%d" % int(time.time()))

# 假音频：只验「字节是否原样透传」，不需要是合法 WAV
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(range(48))
seen: list[str] = []


class FakeTTS(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        seen.append(self.path)
        if self.path.startswith("/bad"):
            body = json.dumps({"error": "missing text"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(WAV)))
        self.end_headers()
        self.wfile.write(WAV)

    def log_message(self, *args):  # 静音
        return


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    os.environ["ZHIBAN_DATA_DIR"] = str(TMP)

    from backend.db.connection import get_db
    from backend.errors import AppError
    from backend.services.settings_service import SettingsService
    from backend.services.tts import get_tts_service

    get_db().migrate()   # 临时库要先建表（Database 构造不自动迁移）

    srv = HTTPServer(("127.0.0.1", 8799), FakeTTS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    PASS: list[str] = []
    FAIL: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (PASS if cond else FAIL).append(name)
        print(f"  {'✅' if cond else '❌'} {name}" + (f"  | {detail}" if detail and not cond else ""))

    svc = SettingsService.get_instance()
    tts = get_tts_service()
    try:
        # ① 未填地址模板
        svc.update({"tts": {"enabled": True, "mode": "custom", "custom_url": ""}})
        try:
            tts.synth_custom("你好")
            check("① 未填地址模板 → 报错", False, "居然没报错")
        except AppError as e:
            blob = f"{e.message or ''}{e.detail or ''}"
            check("① 未填地址模板 → 4002 且说清去哪填",
                  e.code == 4002 and "设置" in blob, f"{e.code} / {blob}")

        # ② GET 模板替换 + 音频透传
        seen.clear()
        svc.update({"tts": {"enabled": True, "mode": "custom", "custom_method": "GET",
                            "custom_format": "wav",
                            "custom_url": "http://127.0.0.1:8799/tts?text={text}&text_lang=zh"}})
        data, ctype = tts.synth_custom("你好 世界/测试")
        check("② GET：音频字节原样返回",
              data == WAV and ctype.startswith("audio/"), f"{len(data)}B / {ctype}")
        want = "text=%E4%BD%A0%E5%A5%BD%20%E4%B8%96%E7%95%8C%2F%E6%B5%8B%E8%AF%95"
        check("② {text} 按 URL 编码替换（中文/空格/斜杠都编码）",
              bool(seen) and want in seen[-1], seen[-1] if seen else "没收到请求")

        # ③ 返回 JSON（非音频）→ 明确诊断
        svc.update({"tts": {"custom_url": "http://127.0.0.1:8799/bad?text={text}"}})
        try:
            tts.synth_custom("你好")
            check("③ 返回非音频 → 报错", False, "居然当作音频返回了")
        except AppError as e:
            check("③ 返回非音频 → 4003 且报出真实 Content-Type",
                  e.code == 4003 and "不是音频" in (e.detail or ""), f"{e.code} / {e.detail}")

        # ④ 端口不通
        svc.update({"tts": {"custom_url": "http://127.0.0.1:8798/tts?text={text}"}})
        try:
            tts.synth_custom("你好")
            check("④ 端口不通 → 报错", False, "居然成功了")
        except AppError as e:
            check("④ 端口不通 → 4003 且回显地址模板",
                  e.code == 4003 and "8798" in (e.detail or ""), f"{e.code} / {e.detail}")
    finally:
        srv.shutdown()

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  失败：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
