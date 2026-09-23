"""课件可视化渲染自测：无头 Chrome 加载 vendor + visual.js，验证 P1/P2/P3。

不依赖啃书先生后端。起一个本地静态服务（serve src/web），加载 vendor 与 visual.js、
markdown.js（含 MD.mindmap），真实调用 window.Viz.render / window.MD.mindmap，
断言返回值与 DOM，并整页截图。

用法::

    .venv/Scripts/python.exe dev/viz_harness_check.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]

# ── 测试用的 Chrome 用户数据目录 ────────────────────────────────
# 硬约束：必须**纯 ASCII**（中文路径下 Chrome 行为不稳定），因此不能放项目内
# （本项目路径含中文）。历史上写死在 C 盘的临时目录，每跑一次留 4 个约 15MB 的 profile、
# 且从不清理 → 累积 3.6 GB 把 C 盘吃满（2026-09-18 用户清理 C 盘时发现）。
# 现在统一落到 Q 盘纯 ASCII 目录；ui_profile() 每次启动还会清掉 24 小时前的旧目录。
UI_TMP = Path(os.environ.get("MRNIBBLE_TEST_TMP") or "Q:/mrnibble_tmp")


def ui_profile(kind: str) -> str:
    """返回一个新的 Chrome 用户数据目录（绝对路径、纯 ASCII）。

    顺手清理**24 小时前**的旧目录：只删老的，正在跑的这次不会被误伤。
    清理失败一律忽略 —— 沙箱有批量删除护栏，被拦下只是留着占地方，不影响测试。
    """
    UI_TMP.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 24 * 3600
    for old in UI_TMP.glob("ui-%s-*" % kind):
        # ⚠️ 删之前必须数文件数：沙箱护栏超阈值（120）时**直接终止进程**，不抛异常，
        # try/except 兜不住 —— 症状是整套测试跑完却打印不出统计行（exit=1、无 traceback）。
        # 一个 Chrome profile 轻松几百个文件，所以这里宁可留着也不删。
        try:
            if old.stat().st_mtime >= cutoff:
                continue
            if sum(1 for _ in old.rglob("*")) > 100:
                continue
            shutil.rmtree(old, ignore_errors=True)
        except Exception:  # noqa: BLE001 - 清理失败绝不能影响测试
            pass
    return str(UI_TMP / ("ui-%s-%d" % (kind, int(time.time()))))
WEB = ROOT / "src" / "web"
SHOT = ROOT.parent / "调试截图" / "课件可视化-渲染验证.png"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{('  | ' + detail) if (detail and not cond) else ''}")


HARNESS_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>viz harness</title>
<link rel="stylesheet" href="/static/vendor/katex/katex.min.css">
<link rel="stylesheet" href="/static/vendor/highlight/github.min.css">
<link rel="stylesheet" href="/static/css/app.css">
</head><body>
<div class="board slides" style="padding:14px">
  <div class="card"><div class="card-kind">好 mermaid</div><div id="mermaid-good" class="viz-box"></div></div>
  <div class="card"><div class="card-kind">好 bar 图</div><div id="chart-good" class="viz-box"></div></div>
  <div class="card lesson-outline-card"><div class="card-kind">本讲导览</div><div id="mindmap-good"></div></div>
  <div class="card"><div class="card-kind">坏 mermaid（回退）</div><div id="mermaid-bad" class="viz-box"></div></div>
  <div class="card"><div class="card-kind">坏 chart NaN（回退）</div><div id="chart-bad" class="viz-box"></div></div>
</div>
<!-- 依赖顺序：d3 → katex → marked → purify → highlight → mermaid → echarts → markmap → markdown → visual -->
<script src="/static/vendor/d3/d3.min.js"></script>
<script src="/static/vendor/katex/katex.min.js"></script>
<script src="/static/vendor/marked/marked.min.js"></script>
<script src="/static/vendor/dompurify/purify.min.js"></script>
<script src="/static/vendor/highlight/highlight.min.js"></script>
<script src="/static/vendor/mermaid/mermaid.min.js"></script>
<script src="/static/vendor/echarts/echarts.min.js"></script>
<script src="/static/vendor/markmap/markmap-view.js"></script>
<script src="/static/vendor/markmap/markmap-lib.js"></script>
<script src="/static/js/markdown.js"></script>
<script src="/static/js/visual.js"></script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        p = urllib.parse.urlparse(self.path).path
        if p in ("/", ""):
            body = HARNESS_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p.startswith("/static/"):
            f = WEB / p[len("/static/"):]
            if f.is_file():
                data = f.read_bytes()
                mt, _ = mimetypes.guess_type(str(f))
                self.send_response(200)
                self.send_header("Content-Type", mt or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_error(404)

    def log_message(self, *a):  # 安静
        pass


def run_server(port: int) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    run_server.state = srv  # type: ignore


async def main() -> int:
    port = 8799
    run_server(port)
    # 纯 ASCII 用户数据目录（中文路径下 Chrome 行为不稳定）
    ud = ui_profile("viz")
    try:
        os.makedirs(ud, exist_ok=True)
    except Exception:
        pass
    chrome = CHROME
    proc = subprocess.Popen(  # noqa: F821
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-proxy-server",
         f"--remote-debugging-port={port + 100}", f"--user-data-dir={ud}",
         "--window-size=1280,1700", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: F821
    try:
        import httpx
        dport = port + 100
        ver = None
        for _ in range(60):
            try:
                ver = httpx.get(f"http://127.0.0.1:{dport}/json/version", timeout=2).json()
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not ver:
            check("CDP 端口可达", False, "chrome 未启动")
            return 1
        check("CDP 端口可达", True)

        async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=None, ping_interval=None) as bws:
            _id = [0]

            def nid():
                _id[0] += 1
                return _id[0]

            async def bsend(method, params=None):
                i = nid()
                await bws.send(json.dumps({"id": i, "method": method, "params": params or {}}))  # noqa: F821
                return i

            async def bwait(i, t=20):
                while True:
                    r = json.loads(await asyncio.wait_for(bws.recv(), t))  # noqa: F821
                    if r.get("id") == i:
                        return r

            r = await bsend("Target.createTarget", {"url": f"http://127.0.0.1:{port}/"})
            tid = (await bwait(r))["result"]["targetId"]
            pws = None
            for _ in range(40):
                lst = httpx.get(f"http://127.0.0.1:{dport}/json/list", timeout=3).json()
                t = next((x for x in lst if x.get("id") == tid), None)
                if t and t.get("webSocketDebuggerUrl"):
                    pws = t["webSocketDebuggerUrl"]
                    break
                await asyncio.sleep(0.3)
            if not pws:
                check("页面调试端点", False)
                return 1
            check("页面调试端点", True)

        async with websockets.connect(pws, max_size=None, ping_interval=None) as ws:
            _id = [0]

            def nid():
                _id[0] += 1
                return _id[0]

            pending: dict = {}

            async def reader():
                while True:
                    try:
                        raw = await ws.recv()
                    except Exception:
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if "id" in msg and msg["id"] in pending:
                        pending[msg["id"]].set_result(msg)
                    else:
                        mm = msg.get("method", "")
                        if mm == "Runtime.exceptionThrown":
                            ex = msg.get("params", {}).get("exceptionDetails", {})
                            pending.setdefault("__exceptions", []).append(
                                ex.get("exception", {}).get("description")
                                or ex.get("text", "unknown"))

            _task = asyncio.create_task(reader())

            async def ev(expr, t=30):
                i = nid()
                loop = asyncio.get_event_loop()
                fut = loop.create_future()
                pending[i] = fut
                await ws.send(json.dumps({"id": i, "method": "Runtime.evaluate",
                                          "params": {"expression": expr, "returnByValue": True,
                                                     "awaitPromise": True}}))
                try:
                    res = await asyncio.wait_for(fut, t)
                finally:
                    pending.pop(i, None)
                if "error" in res:
                    raise RuntimeError(str(res["error"]))
                return res.get("result", {}).get("result", {}).get("value")

            await ws.send(json.dumps({"id": nid(), "method": "Runtime.enable"}))
            await ws.send(json.dumps({"id": nid(), "method": "Page.enable"}))

            async def cmd(method, params=None, t=15):
                i = nid()
                loop = asyncio.get_event_loop()
                fut = loop.create_future()
                pending[i] = fut
                await ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
                try:
                    return await asyncio.wait_for(fut, t)
                finally:
                    pending.pop(i, None)
            # 等 vendor/visual/markdown 加载完
            ok = False
            for _ in range(40):
                ok = await ev("(function(){return !!(window.mermaid && window.echarts "
                              "&& window.MD && window.MD.mindmap && window.Viz && window.Viz.render);})()",
                              t=10)
                if ok:
                    break
                await asyncio.sleep(0.3)
            check("vendor + visual + markdown 加载就绪", bool(ok))

            # 跑全部断言（页面内 async IIFE）
            res = await ev(TEST_EXPR, t=40)
            items = (res or {}).get("results", []) if isinstance(res, dict) else []
            for it in items:
                check(it.get("name", "?"), it.get("ok"), it.get("detail", ""))

            # 截图（整页）
            try:
                os.makedirs(str(SHOT.parent), exist_ok=True)
                m = await cmd("Page.captureScreenshot",
                              {"format": "png", "captureBeyondViewport": True}, t=20)
                data = (m.get("result") or {}).get("data")
                if data:
                    SHOT.write_bytes(base64.b64decode(data))
                    print(f"  📸 截图已存：{SHOT}  ({SHOT.stat().st_size} bytes)")
                else:
                    print("  ⚠️ 截图未获取到数据")
            except Exception as e:
                print(f"  ⚠️ 截图失败：{e}")

            _task.cancel()
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            run_server.state.shutdown()  # type: ignore
        except Exception:
            pass

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  - " + f)
    return 1 if FAIL else 0


TEST_EXPR = r"""
(async () => {
  const results = [];
  const check = (name, cond, detail) => results.push({name, ok: !!cond, detail: (detail==null?"":String(detail))});
  const waitSel = (host, sel, t=8000) => new Promise((res) => {
    const t0 = Date.now();
    (function poll(){
      if (host.querySelector(sel)) return res(true);
      if (Date.now()-t0 > t) return res(false);
      setTimeout(poll, 80);
    })();
  });
  const awaitR = async (r) => (r && r.then) ? await r : r;

  check("mermaid 全局存在", typeof window.mermaid !== "undefined");
  check("echarts 全局存在", typeof window.echarts !== "undefined");
  check("MD.mindmap 存在", !!(window.MD && typeof window.MD.mindmap === "function"));
  check("Viz.render 存在", !!(window.Viz && typeof window.Viz.render === "function"));

  // 1. 好 mermaid
  {
    const host = document.getElementById("mermaid-good");
    let r = window.Viz.render(host, {kind:"diagram", title:"t", bullets:["x"],
      diagram:{lang:"mermaid", code:"flowchart TD\n  A[开始] --> B[处理]\n  B --> C[结束]"}});
    r = await awaitR(r);
    const has = await waitSel(host, "svg");
    check("好 mermaid → 返回 svg", r === "svg", "r="+r);
    check("好 mermaid → DOM 出现 svg", has);
  }
  // 2. 坏 mermaid
  {
    const host = document.getElementById("mermaid-bad");
    let r = window.Viz.render(host, {kind:"diagram", title:"t", bullets:["x"],
      diagram:{lang:"mermaid", code:"flowchart TD\nA[未闭合"}});
    r = await awaitR(r);
    const fb = host.querySelector(".viz-fallback");
    check("坏 mermaid → 返回 fallback", r === "fallback", "r="+r);
    check("坏 mermaid → DOM 出现 .viz-fallback", !!fb, "html=" + host.innerHTML.slice(0, 160));
  }
  // 3. 好 bar
  {
    const host = document.getElementById("chart-good");
    let r = window.Viz.render(host, {kind:"chart", title:"成绩", bullets:["x"],
      chart:{type:"bar", title:"成绩", unit:"分", categories:["甲","乙"],
             series:[{name:"数学", data:[88,92]}]}});
    r = await awaitR(r);
    const cv = host.querySelector(".chart-box canvas");
    check("好 bar → 返回 canvas", r === "canvas", "r="+r);
    check("好 bar → DOM 出现 canvas", !!cv);
  }
  // 4. 坏的 chart（NaN）
  {
    const host = document.getElementById("chart-bad");
    let r = window.Viz.render(host, {kind:"chart", title:"t", bullets:["x"],
      chart:{type:"bar", categories:["a","b"], series:[{name:"s", data:[12, NaN]}]}});
    r = await awaitR(r);
    const fb = host.querySelector(".viz-fallback");
    check("坏 chart(NaN) → 返回 fallback", r === "fallback", "r="+r);
    check("坏 chart(NaN) → DOM 出现 .viz-fallback", !!fb);
  }
  // 5. mindmap 本讲导览
  {
    const host = document.getElementById("mindmap-good");
    let svg = null;
    try { svg = window.MD.mindmap(host, "- 要点一\n  - 术语A：说明\n- 要点二"); } catch(e){}
    const has = await waitSel(host, "svg", 8000);
    check("MD.mindmap(outline) → svg 出现", has, "svg="+(!!svg));
  }
  // 6. P1 特色页：对比表格 / 金句卡（renderExtras）
  {
    check("Viz.renderExtras 存在", typeof window.Viz.renderExtras === "function");
    const tb = document.createElement("div");
    const rt = window.Viz.renderExtras(tb, {kind:"table",
      table:{title:"936 与 65001 对照", columns:["项目","936","65001"],
             rows:[["编码","GBK","UTF-8"],["中文占用","2 字节","3 字节"]]}});
    check("好表格 → 返回 table", rt === "table", "r="+rt);
    check("好表格 → DOM 出 .viz-table 且列/行数正确",
          !!tb.querySelector(".viz-table")
          && tb.querySelectorAll("thead th").length === 3
          && tb.querySelectorAll("tbody tr").length === 2
          && tb.querySelectorAll("tbody td").length === 6,
          "th=" + tb.querySelectorAll("thead th").length
          + " tr=" + tb.querySelectorAll("tbody tr").length);
    check("好表格 → 表题渲染",
          ((tb.querySelector(".table-title") || {}).textContent || "") === "936 与 65001 对照");
    check("好表格 → 单元格内容就位",
          (tb.querySelectorAll("tbody td")[1] || {}).textContent === "GBK");

    const tk = document.createElement("div");
    const rk = window.Viz.renderExtras(tk, {kind:"takeaway", takeaway:"先分类，再选工具。"});
    check("金句卡 → 返回 takeaway", rk === "takeaway", "r="+rk);
    check("金句卡 → DOM 出 .takeaway-box 且文案就位",
          ((tk.querySelector(".takeaway-box") || {}).textContent || "") === "先分类，再选工具。");

    const bad = document.createElement("div");
    const rb = window.Viz.renderExtras(bad, {kind:"table",
      table:{title:"坏", columns:["甲","乙","丙"], rows:[["只有两列","少一列"]]}});
    check("坏表格（行列不齐）→ 不渲染、返回空",
          rb === "" && bad.children.length === 0, "r="+rb);
    const none = document.createElement("div");
    check("普通页 → 返回空且不产生 DOM",
          window.Viz.renderExtras(none, {kind:"concept", title:"t"}) === ""
          && none.children.length === 0);
  }
  // 7. 架构化图示：后端已编译好的 SVG 直接内联（Archify 式管线的前端出口）
  {
    const host = document.createElement("div");
    const backendSvg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100" '
      + 'class="zf-svg" data-diagram="workflow" data-preset="classic">'
      + '<g class="zf-node" data-id="a"><rect x="10" y="10" width="80" height="40" rx="8"/>'
      + '<text x="50" y="35">运行 chcp</text></g></svg>';
    const r = window.Viz.render(host, {kind: "diagram", diagram: {svg: backendSvg, preset: "classic"}});
    check("后端 SVG → 直接内联（返回 svg）", r === "svg", "r=" + r);
    check("内联后 DOM 出现 .zf-svg", !!host.querySelector("svg.zf-svg"));
    check("内联后节点与文字就位",
          host.querySelectorAll(".zf-node").length === 1
          && (host.textContent || "").indexOf("运行 chcp") >= 0);
    check("后端 SVG 不需要 mermaid 参与（节点直接可用）",
          host.querySelector("g.zf-node rect") !== null);

    const evil = document.createElement("div");
    const r2 = window.Viz.render(evil, {kind: "diagram",
      diagram: {svg: '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'}});
    check("含 script 的 SVG 被拒绝并回退要点",
          r2 === "fallback" && !!evil.querySelector(".viz-fallback"), "r=" + r2);

    const legacyHost = document.createElement("div");
    const r3 = window.Viz.render(legacyHost, {kind: "diagram",
      diagram: {lang: "mermaid", code: "flowchart TD\n  A[甲] --> B[乙]"}});
    check("旧 Mermaid 形态仍走老路径（向后兼容）", !!r3, "r=" + r3);
  }
  return {results};
})()
"""


def _bootstrap() -> int:
    return asyncio.run(main())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
