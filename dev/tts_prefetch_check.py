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
DATA = ROOT / ".tmp" / ("test-data-prefetch-%d" % int(time.time()))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# 用户数据目录放 Q 盘**纯 ASCII** 路径（中文路径下 Chrome 行为不稳定）；
# 详见 course_ui_check.py 里 UI_TMP / ui_profile() 的说明。本脚本用固定目录 + 主动清理。
UI_TMP = Path(os.environ.get("MRNIBBLE_TEST_TMP") or "Q:/mrnibble_tmp")
UD = str(UI_TMP / ("ui-prefetch-%d" % int(time.time())))
APPPORT = 8769
CDP_PORT = 9229
SYNTH_DELAY = 0.8          # 假端点单次合成耗时
AUDIO_SECONDS = 1.5        # 每段音频时长

PASS: list[str] = []
FAIL: list[str] = []
HITS: list[tuple[float, str, bool]] = []   # (到达时刻, 请求文本, 是否成功)
CONC = {"now": 0, "max": 0}                # 假端点同时在处理的请求数（有界并发的证据）
FAIL_MODE = {"mode": "none"}               # none | first-per-text | all（模拟上游拥堵）
_FAILED_TEXTS: set[str] = set()
SYNTH_DELAY_CUR = [SYNTH_DELAY]            # 假端点单次合成耗时（按阶段可调）


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
    """假语音端点：记录请求 → 按 FAIL_MODE 决定成功/失败 → 睡 SYNTH_DELAY → 返回真 WAV。

    FAIL_MODE 复现用户 2026-09-21 实测的场景（API 临时拥堵）：
    - ``first-per-text``：每段文本的**第一次**请求失败（500 类临时错误），之后成功
      —— 验证「单段自动重试后自愈，不再整堂停工」；
    - ``all``：全部失败 —— 验证「重试耗尽 → 停机保留现场 → retry() 恢复」。
    """

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            text = str(json.loads(raw.decode("utf-8")).get("input") or "")
        except Exception:  # noqa: BLE001
            text = ""
        mode = FAIL_MODE["mode"]
        if mode == "all" or (mode == "first-per-text" and text not in _FAILED_TEXTS):
            if mode == "first-per-text":
                _FAILED_TEXTS.add(text)
            HITS.append((time.time(), text, False))
            self.send_response(500)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "simulated upstream congestion"}')
            return
        HITS.append((time.time(), text, True))
        CONC["now"] += 1
        CONC["max"] = max(CONC["max"], CONC["now"])
        try:
            time.sleep(SYNTH_DELAY_CUR[0])
            body = wav_bytes()
            self.send_response(200)
            self.send_header("content-type", "audio/wav")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            CONC["now"] -= 1

    def log_message(self, *a):  # 静音访问日志
        pass


TEXT = ("中文路径本身没有问题。问题在于不要把中文写进批处理文件。"
        "批处理文件应当只包含 ASCII 字符。如果需要输出中文，请改用 PowerShell。"
        "另外，chcp 65001 只是切换代码页，并不能解决文件本身编码不一致的问题。"
        "所以遇到乱码时，先检查文件的编码，再检查代码页设置。")

# 各阶段用**不同文本**：_prime 是跨调用缓存，同一文本第二次 speak 会全程命中缓存，
# 失败注入就失效了（阶段之间还会 Voice.stop() 清缓存，双保险）。
TEXT3 = ("浏览器会缓存合成结果，跨页切换时第一段不需要重新合成。"
         "因此每一页讲稿都应该尽早送进预热，让播放与合成真正并行。"
         "失败的那一段要能原地重试，而不是让整堂课停在那里不动。")
TEXT4 = ("当上游服务临时拥堵时，合成请求可能连续失败。"
         "此时应当退避后重试几次，给服务恢复的时间；重试仍然失败才告知用户，"
         "并且保留现场，让用户可以一键从失败的那一句继续朗读，而不是整堂课报废。")
PRIME_MARK = "预热第"
SPEAK_MARK = "朗读第"
# ⚠️ 每句必须**唯一**：fetchCached 按文本去重，同一句重复 N 遍只会发 1 个请求，
# 竞争场景就构造不出来了（第一版就是重复句，prime 只发出 1 个请求，断言 vacuous 通过）。
# chunkChars=40 时一句一段：PRIME_TEXT → 40 个互异请求，一次性占满连接队列。
PRIME_TEXT = "".join(
    f"预热第{i:02d}句，这一句与众不同，用来占满连接队列的第{i:02d}个请求。"
    for i in range(40))
SPEAK_TEXT = "".join(
    f"朗读第{i:02d}句，当前页真正要播放的第{i:02d}段内容，不能被预热挤到后面。"
    for i in range(6))

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

    # DATA 带时间戳每轮全新；不做清理（rmtree 撞沙箱 turn 级删除护栏会终止进程）。
    DATA.mkdir(parents=True)

    env = {**os.environ, "MRNIBBLE_DATA_DIR": str(DATA), "MRNIBBLE_PORT": str(APPPORT),
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

                    # ── [3] 单段合成失败 → 自动重试后自愈（不再整堂停工）──
                    # 复现用户 2026-09-21 实测：API 临时拥堵 → 合成失败 → 旧行为
                    # 直接停工、点暂停/继续都没反应，只能停止整堂课重来。
                    await ev("Voice.stop(); void 0")
                    await ev("window.__log.length = 0; void 0")
                    FAIL_MODE["mode"] = "first-per-text"
                    _FAILED_TEXTS.clear()
                    HITS.clear()
                    await ev("window.__retries = 0; window.__err3 = null; window.__end3 = 0;")
                    t3 = time.time()
                    res3 = await ev("""
                      (async () => {
                        await Voice.speak(%s, {chunkChars: 40,
                          onChunk: () => {},
                          onRetry: () => { window.__retries++; },
                          onError: (m) => { window.__err3 = m; },
                          onEnd: () => { window.__end3 = 1; }});
                        return {retries: window.__retries, err: window.__err3,
                                end: window.__end3};
                      })()""" % json.dumps(TEXT3, ensure_ascii=False))
                    log3 = await ev("window.__log") or []
                    n500 = sum(1 for h in HITS if not h[2])
                    print(f"  [3] 500x{n500}，重试 {(res3 or {}).get('retries')} 次，"
                          f"耗时 {time.time() - t3:.1f}s")
                    check("3.1 单段首次合成失败被自动重试并自愈（用户侧无错误、正常结束）",
                          bool(res3) and not res3.get("err") and res3.get("end") == 1,
                          str(res3))
                    check("3.2 失败真的发生过且重试被触发（防空断言）",
                          n500 >= 1 and (res3 or {}).get("retries", 0) >= 1,
                          f"500x{n500} retries={(res3 or {}).get('retries')}")
                    check("3.3 重试后所有段都播了出来", len(log3) >= 3, f"{len(log3)} 段")

                    # ── [4] 重试也耗尽 → 停机保留现场，「重试朗读」能接着跑 ──
                    await ev("Voice.stop(); void 0")
                    await ev("window.__log.length = 0; void 0")
                    FAIL_MODE["mode"] = "all"
                    HITS.clear()
                    await ev("window.__err4 = null; window.__errCount = 0; window.__end4 = 0;")
                    t4 = time.time()
                    res4 = await ev("""
                      (async () => {
                        await Voice.speak(%s, {chunkChars: 40,
                          onChunk: () => {},
                          onError: (m) => { window.__err4 = m; window.__errCount++; },
                          onEnd: () => { window.__end4 = 1; }});
                        return {err: window.__err4, errCount: window.__errCount,
                                end: window.__end4, parked: Voice.parked()};
                      })()""" % json.dumps(TEXT4, ensure_ascii=False))
                    print(f"  [4] 停机耗时 {time.time() - t4:.1f}s，err={str((res4 or {}).get('err'))[:80]}")
                    check("4.1 重试耗尽后明确报错（含重试次数与恢复提示）",
                          bool(res4) and res4.get("errCount") == 1
                          and "已自动重试" in str(res4.get("err") or "")
                          and "重试朗读" in str(res4.get("err") or ""),
                          str(res4)[:200])
                    check("4.2 报错后朗读停机但**保留现场**（parked=true，未标记讲完）",
                          bool(res4) and res4.get("parked") is True
                          and res4.get("end") == 0, str(res4)[:200])
                    # 端点恢复 → 模拟用户点「↻ 重试朗读」
                    FAIL_MODE["mode"] = "none"
                    HITS.clear()
                    res4b = await ev("""
                      (async () => {
                        await Voice.retry();
                        return {end: window.__end4, errCount: window.__errCount,
                                parked: Voice.parked()};
                      })()""")
                    check("4.3 端点恢复后 retry() 从失败的那句继续并播完（不再需要停止整堂课）",
                          bool(res4b) and res4b.get("end") == 1
                          and res4b.get("errCount") == 1
                          and res4b.get("parked") is False, str(res4b))

                    # ── [5] 预热不饿死播放页（有界并发 + 流水线优先）──
                    # 旧行为：prime 一次性发出几十个请求，占满浏览器对同源的 6 条连接，
                    # 当前页第一段的合成请求排在队列后面 → 页首白等一整个合成周期。
                    await ev("Voice.stop(); void 0")
                    SYNTH_DELAY_CUR[0] = 1.2
                    FAIL_MODE["mode"] = "none"
                    HITS.clear()
                    CONC["now"] = 0
                    CONC["max"] = 0
                    await ev("window.__err5 = null; window.__end5 = 0; window.__t0 = 0;")
                    res5 = await ev("""
                      (async () => {
                        // 复现 speakSlide 的顺序：先预热下一页，再朗读本页
                        window.__t0 = Date.now();
                        Voice.prime(%s, {chunkChars: 40});
                        await Voice.speak(%s, {chunkChars: 40,
                          onChunk: () => {},
                          onError: (m) => { window.__err5 = m; },
                          onEnd: () => { window.__end5 = 1; }});
                        return {t0: window.__t0, end: window.__end5, err: window.__err5};
                      })()""" % (json.dumps(PRIME_TEXT, ensure_ascii=False),
                                json.dumps(SPEAK_TEXT, ensure_ascii=False)))
                    speak_hits = [h for h in HITS if SPEAK_MARK in h[1]]
                    prime_n = len(HITS) - len(speak_hits)
                    first_gap = ((speak_hits[0][0] * 1000 - (res5 or {}).get("t0", 0))
                                 if speak_hits and res5 else 99999)
                    print(f"  [5] 预热 {prime_n} 个请求；播放页首要段 {first_gap:.0f}ms 到达；"
                          f"假端点最大并发 {CONC['max']}")
                    check("5.1 播放页第一段没有排在几十个预热请求后面（< 3s 到达假端点）",
                          0 < first_gap < 3000,
                          f"首要段 {first_gap:.0f}ms（预热 {prime_n} 个请求；"
                          f"旧实现下要排到连接队列尾部，约 {prime_n // 6 * 1200}ms+）")
                    check("5.2 合成并发有界（假端点同时在处理 ≤ 6）",
                          CONC["max"] <= 6, f"max={CONC['max']}")
                    check("5.3 有预热抢占的情况下朗读仍正常结束",
                          bool(res5) and not res5.get("err") and res5.get("end") == 1,
                          str(res5)[:160])
            return res, elapsed, log

        res, elapsed, log = asyncio.run(_run())
        res = res or {}
        check("1.2 speak 无错误且正常结束",
              not res.get("err") and res.get("end") == 1, str(res))
        check("1.3 被切成多段（≥3）", (res.get("chunks") or 0) >= 3, str(res.get("chunks")))

        gaps = [round((HITS[i + 1][0] - HITS[i][0]) * 1000) for i in range(len(HITS) - 1)] if len(HITS) >= 2 else []
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

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
