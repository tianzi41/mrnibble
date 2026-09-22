"""中英双语切换·第一期 验收套件（可重复跑）。

用无头 Chrome 起「源码态」后端，断言语言切换基础设施工作正常：
  1. 页面正常渲染（无 JS 视图错误 data-view-error）；
  2. 顶栏出现语言切换控件（#lang-sel，含 中 / EN 两个选项）；
  3. 默认语言为中文：html lang=zh 且 localStorage 默认 zh，导航含「课程」；
  4. 点击 EN 后导航文字变英文（含 Courses，不含「课程」）；
  5. 切换后 localStorage 持久化（zhiban-lang=en）；
  6. 切换后 html lang=en；
  7. 刷新页面后仍是英文（localStorage 持久化 → 重载后导航含 Courses、html lang=en）；
  8. 语言切换器 title 用 t() 取自身文案（lang.label）。

用法::

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe dev/i18n_check.py

落盘日志：.tmp/i18n_check.log
临时目录：Q:/zhiban_tmp/i18n-probe-<ts>（纯 ASCII，删除前数文件数超 100 跳过）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
PORT = 8768
CDP = 9238
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# 纯 ASCII 临时目录（沙箱护栏：删除前数文件数，超过 100 跳过）
TMPROOT = Path("Q:/zhiban_tmp")
LOG = ROOT / ".tmp" / "i18n_check.log"

# 沙箱删除护栏阈值：超限时它是直接终止进程，所以清理前必须自己数文件数。
_WIPE_LIMIT = 100

PASS: list[str] = []
FAIL: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    log(f"  {'OK' if cond else 'XX'} {name}" + (f"  | {detail}" if (detail and not cond) else ""))


def _safe_wipe(path: Path) -> None:
    try:
        if path.is_dir():
            n = sum(1 for _ in path.rglob("*"))
            if n > _WIPE_LIMIT:
                log(f"  [跳过清理] {path.name}: {n} 个文件超过护栏阈值，留着不影响结论")
                return
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


async def _probe(target: dict, ud: Path) -> None:
    import websockets

    async with websockets.connect(target["webSocketDebuggerUrl"],
                                  max_size=None, ping_interval=None) as ws:

        async def ev(expr, t: float = 30):
            n = [0]

            async def _send():
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": "Runtime.evaluate",
                                          "params": {"expression": expr,
                                                     "returnByValue": True,
                                                     "awaitPromise": True}}))
                while True:
                    r = json.loads(await asyncio.wait_for(ws.recv(), t))
                    if r.get("id") == n[0]:
                        res = r.get("result", {})
                        if res.get("exceptionDetails"):
                            return "__jsError:" + str(res["exceptionDetails"])[:200]
                        return res.get("result", {}).get("value")
            return await _send()

        await ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
        # 等导航渲染出来
        for _ in range(60):
            nav = await ev("Array.from(document.querySelectorAll('#nav button')).map(b=>b.textContent).join('|')")
            if nav and "课程" in str(nav):
                break
            await asyncio.sleep(0.3)

        # A1 页面正常渲染（无 data-view-error）
        err = await ev("!!document.querySelector('[data-view-error]')")
        check("A1 页面正常渲染（无 data-view-error）", not err, str(err))

        # A2 语言切换控件存在且有 中/EN 选项
        sel_info = await ev("""(function(){
          var s = document.getElementById('lang-sel');
          if (!s) return 'no-sel';
          return JSON.stringify({ exists: true,
            options: Array.from(s.options).map(o=>o.value+':'+o.text),
            title: s.title });
        })()""")
        check("A2 顶栏出现语言切换控件（#lang-sel，含 中/EN）",
              '"exists":true' in str(sel_info) and 'en:EN' in str(sel_info) and 'zh:中' in str(sel_info),
              str(sel_info))

        # A3 默认中文：html lang=zh + localStorage 默认 zh
        html_lang = await ev("document.documentElement.getAttribute('lang')")
        ls_default = await ev("(function(){try{return localStorage.getItem('zhiban-lang')}catch(e){return 'ERR:'+e.name}})()")
        check("A3 默认语言为中文（html lang=zh，localStorage 默认 zh）",
              str(html_lang) == "zh" and (ls_default == "zh" or ls_default is None),
              f"lang={html_lang} ls={ls_default}")

        nav_zh = await ev("Array.from(document.querySelectorAll('#nav button')).map(b=>b.textContent).join('|')")
        check("A4 默认导航为中文（含「课程」）", "课程" in str(nav_zh), str(nav_zh))

        # 点击 EN（真实派发 change 事件，走用户路径）
        await ev("""(function(){
          var s = document.getElementById('lang-sel');
          s.value = 'en';
          s.dispatchEvent(new Event('change'));
        })()""")
        # 等重渲染
        for _ in range(40):
            nav = await ev("Array.from(document.querySelectorAll('#nav button')).map(b=>b.textContent).join('|')")
            if nav and "Courses" in str(nav):
                break
            await asyncio.sleep(0.3)
        nav_en = await ev("Array.from(document.querySelectorAll('#nav button')).map(b=>b.textContent).join('|')")
        check("A5 点击 EN 后导航变英文（含 Courses，不含「课程」）",
              "Courses" in str(nav_en) and "课程" not in str(nav_en), str(nav_en))

        # A6 切换后 localStorage 持久化
        ls_en = await ev("(function(){try{return localStorage.getItem('zhiban-lang')}catch(e){return 'ERR:'+e.name}})()")
        check("A6 切换后 localStorage 持久化（zhiban-lang=en）", str(ls_en) == "en", str(ls_en))

        # A7 切换后 html lang=en
        html_lang_en = await ev("document.documentElement.getAttribute('lang')")
        check("A7 切换后 html lang=en", str(html_lang_en) == "en", str(html_lang_en))

        # A8 刷新后仍英文（持久化）
        await ev("location.reload()")
        for _ in range(60):
            nav = await ev("Array.from(document.querySelectorAll('#nav button')).map(b=>b.textContent).join('|')")
            if nav and "Courses" in str(nav):
                break
            await asyncio.sleep(0.3)
        nav_after = await ev("Array.from(document.querySelectorAll('#nav button')).map(b=>b.textContent).join('|')")
        html_after = await ev("document.documentElement.getAttribute('lang')")
        ls_after = await ev("(function(){try{return localStorage.getItem('zhiban-lang')}catch(e){return 'ERR:'+e.name}})()")
        check("A8 刷新后仍英文（localStorage 持久化 → 重载导航含 Courses、html lang=en）",
              "Courses" in str(nav_after) and str(html_after) == "en" and str(ls_after) == "en",
              f"nav={nav_after} lang={html_after} ls={ls_after}")

        # A9 切换器 title 用 t() 取自身文案（lang.label：英文环境应为 "Language"）
        title_en = await ev("document.getElementById('lang-sel').title")
        check("A9 语言切换器 title 用 t() 取自身文案（en → Language）",
              str(title_en) == "Language", str(title_en))


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    UD = TMPROOT / f"i18n-probe-{ts}"
    DATA = TMPROOT / f"i18n-data-{ts}"
    log(f"=== i18n_check 启动 ts={ts} ===")
    _safe_wipe(UD)
    _safe_wipe(DATA)
    UD.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "ZHIBAN_DATA_DIR": str(DATA), "ZHIBAN_PORT": str(PORT),
           "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    be_log = LOG.parent / "i18n_be.log"
    be = subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT), env=env,
                          stdout=open(be_log, "w", encoding="utf-8"),
                          stderr=subprocess.STDOUT)
    chrome = None
    try:
        ready = False
        for _ in range(300):
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/api/health", timeout=3,
                             trust_env=False).status_code < 500:
                    ready = True
                    break
            except Exception:
                time.sleep(0.5)
        check("0.1 后端已就绪", ready)
        if not ready:
            log(f"  [诊断] 后端未在 150s 内就绪，末段日志：")
            try:
                tail = be_log.read_text(encoding="utf-8", errors="replace")[-1500:]
                for line in tail.splitlines()[-25:]:
                    log("    " + line)
            except Exception:
                pass
            return 1

        chrome = subprocess.Popen(
            [CHROME, f"--app=http://127.0.0.1:{PORT}", f"--user-data-dir={UD}",
             "--no-first-run", "--no-default-browser-check", "--no-proxy-server",
             "--disable-http-cache", "--autoplay-policy=no-user-gesture-required",
             "--headless=new", f"--remote-debugging-port={CDP}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        target = None
        for _ in range(60):
            try:
                for x in httpx.get(f"http://127.0.0.1:{CDP}/json/list", timeout=3,
                                   trust_env=False).json():
                    if x.get("type") == "page" and f"127.0.0.1:{PORT}" in (x.get("url") or ""):
                        target = x
                        break
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if target is None:
            check("0.2 找到应用页面（CDP）", False, "CDP 未找到应用页面")
            return 1
        check("0.2 找到应用页面（CDP）", True)
        asyncio.run(_probe(target, UD))
    finally:
        for p in (chrome, be):
            if p is None:
                continue
            try:
                p.kill()
            except Exception:
                pass
        _safe_wipe(UD)
        _safe_wipe(DATA)

    log(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        log("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
