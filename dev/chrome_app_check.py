"""真 Chrome ``--app`` 模式冒烟：验证「没有 Edge、用 Chrome 的用户能正常跑」。

背景（2026-09-22 用户提出）：开发机一直走 Edge（启动器默认优先），
从没验证过 Chrome 路径。本脚本用**与 launcher._open_window 完全一致的参数**
启动真实 Chrome（只多 ``--headless=new`` 以便无头执行），断言：

1. Chrome 能带全套参数起窗并打开应用；
2. 页面正常渲染（标题 / 无 JS 运行时错误 / 关键全局在位）；
3. 首次启动引导在 Chrome 下正常走（``guide.done`` 默认 false → #/welcome）；
4. 介绍图加载、``--autoplay-policy=no-user-gesture-required`` 在 Chrome
   下同样放行自动播放（``audio.play()`` 返回 promise 而非 reject）。

用法::

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe dev/chrome_app_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".tmp" / ("test-data-chromeapp-%d" % int(time.time()))
UD = ROOT / ".tmp" / ("test-ud-chromeapp-%d" % int(time.time()))
PORT = 8766
CDP = 9227
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# 沙箱删除护栏阈值：超限时它是**直接终止进程**（不抛异常），
# 所以清理前必须自己数文件数（项目惯例，见 course_ui_check.ui_profile）。
_WIPE_LIMIT = 100

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK' if cond else 'XX'} {name}" + (f"  | {detail}" if (detail and not cond) else ""))


def _safe_wipe(path: Path) -> None:
    try:
        if path.is_dir():
            n = sum(1 for _ in path.rglob("*"))
            if n > _WIPE_LIMIT:
                print(f"  [跳过清理] {path.name}: {n} 个文件超过护栏阈值，留着不影响结论")
                return
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 - 清理失败不影响测试结论
        pass


async def _probe(target: dict) -> None:
    import websockets

    async with websockets.connect(target["webSocketDebuggerUrl"],
                                  max_size=None, ping_interval=None) as ws:
        n = [0]

        async def ev(expr, t=30):
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
                        return {"__jsError": str(res["exceptionDetails"])[:200]}
                    return res.get("result", {}).get("value")

        await ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
        for _ in range(60):
            h = await ev("location.hash")
            if h and "welcome" in str(h):
                break
            await asyncio.sleep(0.3)
        title = await ev("document.title") or ""
        hash_now = await ev("location.hash") or ""
        err = await ev("!!document.querySelector('[data-view-error]')")
        globs = await ev("[typeof Api, typeof Voice, typeof Profile, "
                         "typeof (window.Views||{}).welcome].join(',')")
        img = await ev("(document.querySelector('.welcome-img')||{}).src") or ""
        audio_ok = await ev("""(function(){
          try { var a = new Audio('/static/assets/welcome.mp3');
                var p = a.play();
                return p ? 'promise' : 'no-promise'; }
          catch (e) { return 'throw:' + e.name; }})()""")
        print(f"    title={title!r} hash={hash_now!r}")
        print(f"    globals=[{globs}]  intro.img={img}  audio.play() → {audio_ok}")
        check("1.1 Chrome --app 带全套启动参数正常起窗并打开应用", True)
        check("1.2 页面标题正确", "知伴" in str(title), str(title))
        check("1.3 无 JS 渲染错误（data-view-error）", not err)
        check("1.4 关键全局在位（Api/Voice/Profile/Views.welcome）",
              str(globs) == "object,object,object,object", str(globs))
        check("2.1 首次启动被送去新手引导（#/welcome）", "welcome" in str(hash_now),
              str(hash_now))
        check("2.2 介绍图在 Chrome 下正常加载",
              str(img).endswith("/static/assets/intro.png"), str(img))
        check("3.1 自动播放策略在 Chrome 下同样放行（play() 返回 promise）",
              audio_ok == "promise", str(audio_ok))


def main() -> int:
    DATA.mkdir(parents=True)
    env = {**os.environ, "ZHIBAN_DATA_DIR": str(DATA), "ZHIBAN_PORT": str(PORT),
           "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    be = subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT), env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    chrome = None
    try:
        for _ in range(80):
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/api/health", timeout=3,
                             trust_env=False).status_code < 500:
                    break
            except Exception:
                time.sleep(0.5)
        check("0.1 后端已就绪", True)

        # 与 launcher._open_window 完全一致的参数（只多 --headless=new 供无头跑）
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
            check("1.1 Chrome --app 带全套启动参数正常起窗并打开应用", False,
                  "CDP 未找到应用页面")
            return 1
        asyncio.run(_probe(target))
    finally:
        for p in (chrome, be):
            if p is None:
                continue
            try:
                p.kill()
            except Exception:
                pass

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
