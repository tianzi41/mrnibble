"""T-预取自检：朗读是否真的「边播边合成下一段」。

用户实测云端朗读每段之间停顿约 2.5 秒。根因不是固定 sleep，而是**串行**
「请求合成 → 等合成返回 → 播放 → 再请求下一段」——把整段合成耗时原样暴露成
段间停顿。修法是预取流水线（PREFETCH=2）。

**为什么不能只断言「请求并发」**：那只是代理指标。用户感知的是**段间静音**
（上一段播完 → 下一段起播）。本脚本两层都量：

1. 假语音端点每次 `sleep 0.8s` 再返回**真 WAV**（约 1.5 秒），并记录请求到达时间；
2. 在页面里包装 `window.Audio`，记录每段音频的起播/结束时刻，算出段间静音。

判据：
- 前两个请求到达间隔 **< 400ms**（预取深度 2 → 前两段几乎同时发出；
  串行实现必然 ≥ 800ms，因为要等第一段播完才发第二个）；
- 最长段间静音 **< 300ms**（修复前约等于一次合成耗时，实测 2500ms）。

用法::

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe dev/tts_prefetch_check.py
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
DATA = ROOT / ".tmp" / "test-data-prefetch"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# 用户数据目录放 Q 盘**纯 ASCII** 路径（中文路径下 Chrome 行为不稳定）；
# 详见 course_ui_check.py 里 UI_TMP / ui_profile() 的说明。本脚本用固定目录 + 主动清理。
UI_TMP = Path(os.environ.get("ZHIBAN_TEST_TMP") or "Q:/zhiban_tmp")
UD = str(UI_TMP / "ui-prefetch")
APPPORT = 8769
CDP_PORT = 9229
SYNTH_DELAY = 0.8          # 假端点单次合成耗时
AUDIO_SECONDS = 1.5        # 每段音频时长

PASS: list[str] = []
FAIL: list[str] = []
HITS: list[float] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{('  | ' + detail) if (detail and not cond) else ''}")


def wav_bytes(seconds: float = AUDIO_SECONDS, rate: int = 22050) -> bytes:
    """生成一段真实可解码的 WAV（不能用假字节，浏览器会 decode 失败）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(1200 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(rate * seconds))))
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    """假语音端点：记录请求到达时间 → 睡 SYNTH_DELAY → 返回真 WAV。"""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        HITS.append(time.time())
        time.sleep(SYNTH_DELAY)
        body = wav_bytes()
        self.send_response(200)
        self.send_header("content-type", "audio/wav")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静音访问日志
        pass


TEXT = ("中文路径本身没有问题。问题在于不要把中文写进批处理文件。"
        "批处理文件应当只包含 ASCII 字符。如果需要输出中文，请改用 PowerShell。"
        "另外，chcp 65001 只是切换代码页，并不能解决文件本身编码不一致的问题。"
        "所以遇到乱码时，先检查文件的编码，再检查代码页设置。")

WRAP_AUDIO = """
window.__log = [];
var OA = window.Audio;
window.Audio = function (u) {
  var a = new OA(u);
  var rec = { start: null, end: null };
  window.__log.push(rec);
  var op = a.play.bind(a);
  a.play = function () { rec.start = performance.now(); return op().catch(function () {}); };
  a.addEventListener("ended", function () { rec.end = performance.now(); });
  return a;
};
"""


def wait_http(url: str, timeout: float = 90.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=3, trust_env=False).status_code < 500:
                return True
        except Exception:
            time.sleep(0.5)
    return False


# 沙箱删除护栏的阈值：单次删除的文件数超过它就会被拦。
# ⚠️ 关键：被拦时它**直接终止进程**，不是抛 Python 异常 ——
# 所以 `except Exception` 和 `rmtree(ignore_errors=True)` 统统兜不住，
# 症状是「断言全跑完 ✅ 却打印不出统计行、exit=1、日志无 traceback」。
# 实测：ui-prefetch 386 个文件时必现。因此**删之前必须自己数文件数**。
_WIPE_FILE_LIMIT = 100


def _safe_wipe(path) -> None:
    """尽力清理临时目录；**无论如何都不能影响测试结论**。"""
    try:
        p = Path(path)
        if p.is_dir():
            n = sum(1 for _ in p.rglob("*"))
            if n > _WIPE_FILE_LIMIT:
                print(f"  [跳过清理] {p.name}: {n} 个文件超过护栏阈值，"
                      "删了会中断脚本；留着不影响结果")
                return
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 - 清理失败与测试结论无关
        pass


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    tts_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[0] 假语音端点 127.0.0.1:{tts_port}/audio/speech"
          f"（每次合成 sleep {SYNTH_DELAY}s，音频 {AUDIO_SECONDS}s）")

    if DATA.exists():
        _safe_wipe(DATA)
    DATA.mkdir(parents=True)
    _safe_wipe(UD)

    env = {**os.environ, "ZHIBAN_DATA_DIR": str(DATA), "ZHIBAN_PORT": str(APPPORT),
           "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    be = subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    proc = None
    try:
        check("0.1 后端已就绪", wait_http(f"http://127.0.0.1:{APPPORT}/api/health"))
        if FAIL:
            return 1

        C = dict(trust_env=False, timeout=40)
        with httpx.Client(**C) as c:
            c.put(f"http://127.0.0.1:{APPPORT}/api/settings",
                  json={"tts": {"mode": "cloud", "enabled": True,
                                "base_url": f"http://127.0.0.1:{tts_port}",
                                "model": "tts-1", "voice": "cixingnansheng"}})
            st = c.get(f"http://127.0.0.1:{APPPORT}/api/tts/status").json()["data"]
        check("0.2 tts 已切到云端且配置就绪",
              st.get("mode") == "cloud" and st.get("cloud_configured") is True, str(st))

        proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                                 "--no-proxy-server",
                                 "--autoplay-policy=no-user-gesture-required",
                                 f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={UD}",
                                 "--window-size=1280,900", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        import websockets
        ver = None
        for _ in range(60):
            try:
                ver = httpx.get(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2,
                                trust_env=False).json()
                break
            except Exception:
                time.sleep(0.5)
        check("0.3 CDP 端口可达", ver is not None)
        if not ver:
            return 1

        async def _run():
            async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=None,
                                          ping_interval=None) as bws:
                _id = [0]

                async def bcmd(m, pr=None):
                    _id[0] += 1
                    await bws.send(json.dumps({"id": _id[0], "method": m, "params": pr or {}}))
                    while True:
                        r = json.loads(await asyncio.wait_for(bws.recv(), 90))
                        if r.get("id") == _id[0]:
                            return r

                t = await bcmd("Target.createTarget", {"url": "about:blank"})
                purl = None
                for _ in range(40):
                    for x in httpx.get(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3,
                                       trust_env=False).json():
                        if x.get("id") == t["result"]["targetId"]:
                            purl = x["webSocketDebuggerUrl"]
                    if purl:
                        break
                    await asyncio.sleep(0.4)

                async with websockets.connect(purl, max_size=None, ping_interval=None) as ws:
                    _n = [0]

                    async def cmd(m, pr=None):
                        _n[0] += 1
                        await ws.send(json.dumps({"id": _n[0], "method": m, "params": pr or {}}))
                        while True:
                            r = json.loads(await asyncio.wait_for(ws.recv(), 90))
                            if r.get("id") == _n[0]:
                                return r

                    async def ev(expr):
                        r = await cmd("Runtime.evaluate",
                                      {"expression": expr, "returnByValue": True,
                                       "awaitPromise": True})
                        res = r.get("result", {})
                        if res.get("exceptionDetails"):
                            return {"__jsError": str(res["exceptionDetails"])[:300]}
                        return res.get("result", {}).get("value")

                    await cmd("Page.enable")
                    await cmd("Runtime.enable")
                    await cmd("Page.navigate", {"url": f"http://127.0.0.1:{APPPORT}/#/settings"})
                    for _ in range(60):
                        if await ev("!!window.Voice"):
                            break
                        await asyncio.sleep(0.3)

                    eng = await ev("(async()=>{await Voice.syncFromServer();return Voice.engine();})()")
                    check("1.1 前端引擎为 cloud", eng == "cloud", str(eng))

                    await ev(WRAP_AUDIO)
                    HITS.clear()
                    t0 = time.time()
                    res = await ev("""
                      (async () => {
                        window.__err = null; window.__end = 0; window.__chunks = 0;
                        await Voice.speak(%s, {chunkChars: 40,
                          onChunk: () => { window.__chunks++; },
                          onError: (m) => { window.__err = m; },
                          onEnd: () => { window.__end = 1; }});
                        return {chunks: window.__chunks, err: window.__err, end: window.__end};
                      })()""" % json.dumps(TEXT, ensure_ascii=False))
                    elapsed = time.time() - t0
                    log = await ev("window.__log") or []
            return res, elapsed, log

        res, elapsed, log = asyncio.run(_run())
        res = res or {}
        check("1.2 speak 无错误且正常结束",
              not res.get("err") and res.get("end") == 1, str(res))
        check("1.3 被切成多段（≥3）", (res.get("chunks") or 0) >= 3, str(res.get("chunks")))

        gaps = [round((HITS[i + 1] - HITS[i]) * 1000) for i in range(len(HITS) - 1)] if len(HITS) >= 2 else []
        print("  相邻请求到达间隔(ms):", gaps)
        check("2.1 前两段请求并发发出（间隔 < 400ms）",
              bool(gaps) and gaps[0] < 400,
              f"gaps={gaps}（串行实现必然 ≥ {int(SYNTH_DELAY * 1000)}ms）")

        sil = [round(log[i + 1]["start"] - log[i]["end"])
               for i in range(len(log) - 1)
               if log[i].get("end") and log[i + 1].get("start")]
        durs = [round(l["end"] - l["start"]) for l in log if l.get("end") and l.get("start")]
        print("  每段播放时长(ms):", durs)
        print("  段间静音(ms):", sil)
        worst = max(sil) if sil else 99999
        check("2.2 段间静音 < 300ms（用户感知的「停顿」）", worst < 300,
              f"最长 {worst}ms（修复前≈合成耗时 {int(SYNTH_DELAY * 1000)}ms，用户实测 2500ms）")
        print(f"  总耗时 {elapsed:.1f}s")
    finally:
        for p in (proc, be):
            if p is None:
                continue
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        srv.shutdown()
        _safe_wipe(DATA)
        _safe_wipe(UD)

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
