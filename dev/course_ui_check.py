"""前端页面冒烟：无头 Chrome 加载课程相关页面，检查是否有 JS 运行时错误。

用法::

    .venv/Scripts/python.exe dev/course_ui_check.py
"""

from __future__ import annotations

import io
import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from make_pdf import make_text_pdf

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".tmp" / "test-data-courseui"
BASE = "http://127.0.0.1:8764"
MOCK = "http://127.0.0.1:8765"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{('  | ' + detail) if (detail and not cond) else ''}")


def wait_http(url: str, timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with httpx.Client(trust_env=False) as cli:
                if cli.get(url, timeout=3).status_code < 500:
                    return True
        except Exception:
            time.sleep(0.6)
    return False


def dump(url: str) -> str:
    """用无头 Chrome 渲染页面并返回 DOM（--no-proxy-server 规避系统代理）。"""
    out = subprocess.run([
        CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-proxy-server", "--virtual-time-budget=6000", "--dump-dom", url,
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    return out.stdout or ""


async def cdp_interactive(base: str, lesson_id: str, first_title: str) -> None:
    """用 CDP 真实操作无头 Chrome，验证：课程详情「接下来」高亮 + 上课/暂停继续/回到课堂浮动入口。

    单脚本一次跑完：本函数内启动 Chrome、驱动交互、最后回收进程，
    避免被沙箱回收进程导致端到端验证中断。
    """
    import asyncio as _asyncio, json, os, subprocess
    import httpx as _hx
    import websockets

    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    # 纯 ASCII 路径；每次用独立 profile，避免跨运行复用 HTTP 缓存导致拿到旧 JS
    ud = "C:/tmp/zhiban_cdp_%d" % int(time.time())
    try: os.makedirs(ud, exist_ok=True)
    except Exception: pass
    port = 9223

    def _check(name, cond, detail=""):
        (PASS if cond else FAIL).append(name)
        print(f"  {'✅' if cond else '❌'} {name}{('  | ' + detail) if (detail and not cond) else ''}")

    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--no-proxy-server", f"--remote-debugging-port={port}",
            f"--user-data-dir={ud}", "--window-size=1280,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ver = None
        for _ in range(60):
            try:
                ver = _hx.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
                break
            except Exception:
                await _asyncio.sleep(0.5)
        if not ver:
            _check("CDP 端口可达", False, "chrome 未启动"); return
        _check("CDP 端口可达", True)

        # 浏览器级 WS：建标签并取页面级 WS 端点
        async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=None, ping_interval=None) as bws:
            _id = [0]
            def nid(): _id[0] += 1; return _id[0]
            async def bsend(method, params=None):
                i = nid(); await bws.send(json.dumps({"id": i, "method": method, "params": params or {}})); return i
            async def bwait(i, t=20):
                while True:
                    r = json.loads(await _asyncio.wait_for(bws.recv(), t))
                    if r.get("id") == i: return r
            r = await bsend("Target.createTarget", {"url": base + "/#/courses"})
            tid = (await bwait(r))["result"]["targetId"]
            pws = None
            for _ in range(40):
                lst = _hx.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json()
                t = next((x for x in lst if x.get("id") == tid), None)
                if t and t.get("webSocketDebuggerUrl"):
                    pws = t["webSocketDebuggerUrl"]; break
                await _asyncio.sleep(0.3)
            if not pws:
                _check("页面调试端点", False); return
            _check("页面调试端点", True)

        # 页面级 WS：真实交互（带事件分发器，便于捕获 console / 异常）
        async with websockets.connect(pws, max_size=None, ping_interval=None) as ws:
            _id = [0]
            def nid(): _id[0] += 1; return _id[0]
            pending = {}
            async def reader():
                while True:
                    try:
                        raw = await ws.recv()
                    except Exception:
                        break
                    try: msg = json.loads(raw)
                    except Exception: continue
                    if "id" in msg and msg["id"] in pending:
                        pending[msg["id"]].set_result(msg)
                    else:
                        mm = msg.get("method", "")
                        if mm in ("Runtime.consoleAPICalled", "Runtime.exceptionThrown"):
                            pass
            _task = _asyncio.create_task(reader())
            async def ev(expr, t=25):
                i = nid()
                loop = _asyncio.get_event_loop()
                fut = loop.create_future()
                pending[i] = fut
                await ws.send(json.dumps({"id": i, "method": "Runtime.evaluate",
                                          "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
                try:
                    res = await _asyncio.wait_for(fut, t)
                finally:
                    pending.pop(i, None)
                if "error" in res: raise RuntimeError(str(res["error"]))
                return res.get("result", {}).get("result", {}).get("value")

            await ws.send(json.dumps({"id": nid(), "method": "Runtime.enable"}))
            await _asyncio.sleep(2.0)

            # ---- 2.40 高亮「接下来要上的下一讲」：进入课程详情 ----
            # 选课程列表里第一个课程项，进入详情（详情才渲染讲次列表）
            await ev("""(function(){var it=document.querySelector('#course-list .item');"""
                    """if(it){it.click();return true;}return false;})()""")
            await _asyncio.sleep(2.0)
            hl = await ev("""(function(){
                var rows=Array.from(document.querySelectorAll('.lesson-row'));
                var hl=rows.filter(function(r){return r.classList.contains('next-lesson');});
                var ft=rows.length?(rows[0].querySelector('.t')||{}).textContent:'';
                var ht=hl.length?(hl[0].querySelector('.t')||{}).textContent:'';
                var pill=document.querySelector('.next-pill');
                return {rowCount:rows.length, hlCount:hl.length,
                        hasPill:!!pill, pillText:pill?pill.textContent:'',
                        firstTitle:ft, hlTitle:ht,
                        firstIsHl:rows.length?rows[0].classList.contains('next-lesson'):false};
            })()""")
            _check("2.40 课程详情存在 next-lesson 高亮", bool(hl and hl.get("hlCount", 0) >= 1), f"hl={hl}")
            _check("2.41 高亮行含「接下来」徽标", bool(hl and hl.get("hasPill") and "接下来" in (hl.get("pillText") or "")), f"hl={hl}")
            _check("2.42 恰好一个讲次被高亮", bool(hl and hl.get("hlCount") == 1), f"hl={hl}")
            _check("2.43 高亮的是第一个未完成讲次",
                   bool(hl and hl.get("hlTitle") and hl.get("hlTitle") != hl.get("firstTitle")),
                   f"first={hl.get('firstTitle') if hl else ''} hl={hl.get('hlTitle') if hl else ''}")
            _check("2.44 已完成的讲次不被高亮", bool(hl and hl.get("firstIsHl") is False), f"hl={hl}")

            # ---- 2.45–2.47 课程详情：讲次下方展示「教学设计」desc 折叠块 ----
            # desc 是大纲阶段生成的（旧课程为 null），详情页整讲列表过去不展示它，
            # 只能点「编辑结构」跳去确认页才看得到 —— 这里锁住「详情页也展示」。
            dd = await ev("""(function(){
                var rows=Array.from(document.querySelectorAll('.item.lesson-row'));
                var ds=Array.from(document.querySelectorAll('.lesson-desc-row'));
                var det=ds.length?ds[0].querySelector('details'):null;
                var txt='';
                if(det){det.open=true;txt=det.textContent||'';}
                return {rowCount:rows.length, descCount:ds.length,
                        afterFirst:(rows.length&&ds.length)?(rows[0].nextElementSibling===ds[0]):false,
                        hasSummary:!!(det&&det.querySelector('summary')),
                        openText:txt.slice(0,300)};
            })()""")
            _check("2.44a 课程详情每个讲次下方都有「教学设计」折叠块",
                   bool(dd and dd["rowCount"] > 0 and dd["descCount"] == dd["rowCount"]),
                   f"rows={dd.get('rowCount') if dd else None} desc={dd.get('descCount') if dd else None}")
            _check("2.44b 折叠块紧跟在讲次行之后（未塞进 flex 行内）",
                   bool(dd and dd.get("afterFirst")), f"afterFirst={dd.get('afterFirst') if dd else None}")
            _check("2.44c 展开后有教学设计字段（学习目标/知识点边界/考察点）",
                   bool(dd and dd.get("hasSummary") and any(
                       k in (dd.get("openText") or "")
                       for k in ("学习目标", "知识点边界", "考察点", "术语口径", "涉及操作"))),
                   f"text={(dd.get('openText') or '')[:80] if dd else None}")

            # ---- C 批：建课向导的课程级「实践环节」开关 ----
            await ev("(function(){var b=document.getElementById('btn-new');"
                     "if(b){b.click();return true;}return false;})()")
            await _asyncio.sleep(1.0)
            wh = await ev("""(function(){
                var s=document.getElementById('f-hands');
                return {exists:!!s,
                        opts: s?Array.prototype.map.call(s.options,function(o){return o.textContent;}) : [],
                        goal: !!document.getElementById('f-goal')};
            })()""")
            _check("2.45 建课向导含「实践环节」开关", bool(wh and wh.get("exists") and wh.get("goal")),
                   f"wh={wh}")
            _check("2.46 实践环节三态：自动 / 包含实操 / 纯理论",
                   bool(wh and len(wh.get("opts") or []) == 3
                        and "自动" in (wh["opts"][0] or "")
                        and any("实操" in (o or "") for o in wh["opts"])
                        and any("纯理论" in (o or "") for o in wh["opts"])),
                   f"opts={wh.get('opts') if wh else None}")
            hands_val = await ev("(document.getElementById('f-hands')||{}).value")
            _check("2.46b 实践环节默认选中「自动」", hands_val == "auto", f"value={hands_val!r}")

            # ---- 课型（学习意图）卡片：主课型必选、辅助课型收在高级选项里 ----
            it = await ev("""(function(){
                var cards=Array.from(document.querySelectorAll('.intent-card'));
                var adv=document.querySelector('details.adv');
                var as=document.getElementById('f-assist');
                var clr=document.getElementById('f-clear-goal');
                var goal=document.getElementById('f-goal');
                return {cards:cards.length,
                        names:cards.map(function(c){var b=c.querySelector('b');return b?b.textContent:'';}),
                        onCount:cards.filter(function(c){return c.classList.contains('on');}).length,
                        advExists:!!adv, advOpen: adv?adv.open:null,
                        assistOpts: as?as.options.length:0,
                        clearBtn: !!clr, goalExists: !!goal};
            })()""")
            _check("2.77 建课向导含课型卡（≥5 张，来自后端课型库）",
                   bool(it and it.get("cards", 0) >= 5 and it.get("goalExists")
                        and "由浅入深精读型" in (it.get("names") or [])
                        and "考点应试型" in (it.get("names") or [])),
                   f"cards={it.get('cards') if it else None} names={it.get('names') if it else None}")
            _check("2.78 高级选项默认收起，内含辅助课型下拉（1 空项 + 9 课型）",
                   bool(it and it.get("advExists") and it.get("advOpen") is False
                        and it.get("assistOpts", 0) == 11),
                   f"advOpen={it.get('advOpen') if it else None} "
                   f"opts={it.get('assistOpts') if it else None}")
            # 交互：选课型后卡片高亮；「清空」真的清掉目标（留空由后端按课型兜底）
            await ev("var c=document.querySelector('.intent-card');if(c)c.click();")
            await _asyncio.sleep(2.5)      # 等按课型写目标的调用回来，再验证清空
            on_after = await ev("document.querySelectorAll('.intent-card.on').length")
            _check("2.79 点课型卡后该卡进入选中态", on_after == 1, f"on={on_after}")
            await ev("var b=document.getElementById('f-clear-goal');if(b)b.click();")
            await _asyncio.sleep(0.4)
            gv = await ev("(document.getElementById('f-goal')||{}).value")
            _check("2.80 「清空」按钮真的清掉目标（允许留空）", gv == "", f"value={gv!r}")
            # ---- P1-1 连点课型卡的「迟到响应」防线（用自研桩制造真实竞态，不依赖后端延迟）----
            await ev("""(function(){
                window.__post = Api.post;
                Api.post = function(url, body){
                    if (String(url).indexOf('suggest-goal') >= 0) {
                        var p = (body||{}).primary;
                        return new Promise(function(res){
                            setTimeout(function(){ res({goal:'GOAL-' + p}); }, 700);
                        });
                    }
                    return window.__post.apply(this, arguments);
                };
                return true;
            })()""")
            # 点第 2 张卡，60ms 后点第 1 张卡：前者响应后到，不得覆盖后者的目标
            await ev("""(function(){
                var c=document.querySelectorAll('.intent-card');
                if(!c || c.length<2) return false;
                c[1].click();
                setTimeout(function(){ c[0].click(); }, 60);
                return true;
            })()""")
            await _asyncio.sleep(2.0)
            g1 = await ev("(document.getElementById('f-goal')||{}).value")
            _check("2.81 连点课型卡：只有最后选中的课型的目标落地（迟到响应被丢弃）",
                   g1 == "GOAL-overview", f"value={g1!r}")
            # 手改目标后，在途的自动写目标同样不得覆盖用户输入
            await ev("""(function(){
                var c=document.querySelectorAll('.intent-card');
                if(!c || !c.length) return false;
                c[0].click();
                var g=document.getElementById('f-goal');
                g.value='我手写的目标';
                g.dispatchEvent(new Event('input'));
                return true;
            })()""")
            await _asyncio.sleep(1.6)
            g2 = await ev("(document.getElementById('f-goal')||{}).value")
            _check("2.82 手改目标后，在途的自动写目标不覆盖用户输入", g2 == "我手写的目标",
                   f"value={g2!r}")
            await ev("if(window.__post){Api.post=window.__post;delete window.__post;}true")

            # ---- P1-2 向导状态不残留：切走再回课程页，不该又被丢进新建向导 ----
            await ev("var b=document.getElementById('btn-new');if(b)b.click();")
            await _asyncio.sleep(0.6)
            in_wizard = await ev("!!document.getElementById('f-go')")
            await ev("location.hash='#/memory'")
            await _asyncio.sleep(0.9)
            await ev("location.hash='#/courses'")
            await _asyncio.sleep(1.4)
            back_wizard = await ev("!!document.getElementById('f-go')")
            _check("2.83 前置：点「新建课程」确实进入了向导", in_wizard is True, f"in={in_wizard}")
            _check("2.84 切到别的页再回课程页：不自动进入新建向导（向导状态不残留）",
                   back_wizard is False, f"f-go={back_wizard}")

            # ---- 本轮新增：课型全量上线 / 单元自定义 / 材料框网格 / 拖拽上传 ----
            await ev("var b=document.getElementById('btn-new');if(b)b.click();")
            await _asyncio.sleep(1.0)
            full = await ev("""(function(){
                var cards=Array.prototype.slice.call(document.querySelectorAll('.intent-card'));
                var names=cards.map(function(c){var b=c.querySelector('b');return b?b.textContent:'';});
                var uc=document.getElementById('f-units-custom');
                var pick=document.getElementById('pick-docs');
                return {cards:cards.length, names:names,
                        unitsCustom: !!uc,
                        customHidden: uc ? uc.style.display === 'none' : null,
                        unitsMax: uc ? uc.max : null,
                        pickDisplay: pick ? getComputedStyle(pick).display : null};
            })()""")
            _check("2.85 课型全量上线（10 张，含诵读涵泳 / 对比阅读 / 微课）",
                   bool(full and full.get("cards") == 10
                        and "诵读涵泳型" in (full.get("names") or [])
                        and "对比阅读型" in (full.get("names") or [])
                        and "微课型" in (full.get("names") or [])),
                   f"cards={full.get('cards') if full else None} "
                   f"names={full.get('names') if full else None}")
            _check("2.86 单元数量含「自定义」数字输入（默认隐藏、上限 12）",
                   bool(full and full.get("unitsCustom") and full.get("customHidden") is True
                        and str(full.get("unitsMax")) == "12"),
                   f"full={full}")
            await ev("(function(){var s=document.getElementById('f-units');"
                     "s.value='custom';s.dispatchEvent(new Event('change'));return true;})()")
            await _asyncio.sleep(0.4)
            shown = await ev("(function(){var u=document.getElementById('f-units-custom');"
                             "return u ? u.style.display !== 'none' : false;})()")
            _check("2.87 选「自定义…」后数字输入框出现", shown is True, f"shown={shown}")
            _check("2.88 材料选择框改为自适应网格（不再是一长条竖列）",
                   bool(full and full.get("pickDisplay") == "grid"),
                   f"display={full.get('pickDisplay') if full else None}")
            await ev("var c=document.getElementById('f-cancel');if(c)c.click();")
            await _asyncio.sleep(0.5)
            await ev("location.hash='#/workbench'")
            await _asyncio.sleep(1.5)
            dd = await ev("""(function(){var d=document.getElementById('side-docs');
                return d ? {drop: !!d.ondrop, over: !!d.ondragover} : null;})()""")
            _check("2.89 工作台「资料库」面板已支持拖拽上传（ondrop + ondragover 已绑定）",
                   bool(dd and dd.get("drop") and dd.get("over")), f"dd={dd}")
            await ev("location.hash='#/courses'")
            await _asyncio.sleep(1.2)

            # 取消向导，回到列表（避免 S.creating 残留影响后续断言）
            await ev("var c=document.getElementById('f-cancel');if(c)c.click();")
            await _asyncio.sleep(0.6)

            # ---- 上课 / 暂停继续 / 回到课堂浮动入口 ----
            await ev("location.hash='#/lessons/%s'" % lesson_id)
            await _asyncio.sleep(3.0)
            snap = """(function(){
                var p = window.LessonProbe ? window.LessonProbe() : null;
                var bs = document.getElementById('b-speak');
                return {probe:p, hash:location.hash,
                        bSpeak: bs?bs.textContent:null,
                        bPause: !!document.getElementById('b-pause'),
                        stageHidden: (document.getElementById('teach-stage')||{}).hidden,
                        sub: (document.getElementById('teach-sub')||{}).textContent||''};
            })()"""
            # 开始上课（优先点弹窗主按钮，否则点操作条 b-speak）
            started = await ev("""(function(){var b=document.querySelector('.modal-box button.primary');"""
                                """if(!b){b=document.getElementById('b-speak');}if(b){b.click();return true;}return false;})()""")
            _check("4.1 点击开始上课成功", bool(started))
            # 轮询 teaching 状态，确认确实进入授课
            teaching = False
            for _ in range(20):
                p = await ev(snap, t=10)
                if p and p.get("probe") and p["probe"].get("teaching"):
                    teaching = True; break
                await _asyncio.sleep(0.2)
            _check("4.2 上课后 teaching=true", teaching is True, f"last={p if p else None}")
            has_pause = await ev("!!document.getElementById('b-pause')")
            _check("4.3 操作条含 b-pause", bool(has_pause))
            vok = await ev("""(function(){return !!(window.Voice && typeof Voice.pause==='function'"""
                            """ && typeof Voice.resume==='function' && typeof Voice.isSpeaking==='function');})()""")
            _check("4.4 Voice 暴露 pause/resume/isSpeaking", bool(vok))

            # 暂停
            await ev("var _p=document.getElementById('b-pause');if(_p)_p.click();")
            await _asyncio.sleep(0.6)
            vp = await ev("window.LessonProbe().voicePaused")
            _check("4.5 暂停后 voicePaused=true", vp is True, f"vp={vp}")
            txt = await ev("(document.getElementById('b-pause')||{}).textContent || ''")
            _check("4.6 按钮文案变「继续」", "继续" in (txt or ""), f"txt={txt}")
            sub = await ev("(document.getElementById('teach-sub')||{}).textContent || ''")
            _check("4.7 字幕显示已暂停", "已暂停" in (sub or ""), f"sub={sub}")

            # 继续
            await ev("var _p=document.getElementById('b-pause');if(_p)_p.click();")
            await _asyncio.sleep(0.6)
            vp2 = await ev("window.LessonProbe().voicePaused")
            _check("4.8 继续后 voicePaused=false", vp2 is False, f"vp2={vp2}")

            # 再次暂停，避免后台朗读推进到讲完触发自动跳页，干扰后续断言
            await ev("var _p=document.getElementById('b-pause');if(_p)_p.click();")
            await _asyncio.sleep(0.3)

            # 切到别的页（记忆），浮动「回到课堂」入口应出现
            await ev("location.hash='#/memory'")
            await _asyncio.sleep(1.0)
            pill = await ev("""(function(){var p=document.getElementById('lesson-return');"""
                            """return p?{exists:true,hidden:p.hidden}:{exists:false};})()""")
            _check("4.9 离开课堂后浮动入口出现",
                   bool(pill and pill.get("exists") and not pill.get("hidden")), f"pill={pill}")

            # 点击回到课堂
            await ev("var _p=document.getElementById('lesson-return');if(_p)_p.click();")
            await _asyncio.sleep(1.3)
            back = await ev("window.LessonProbe()")
            _check("4.10 回到课堂 teaching 仍为 true", bool(back and back.get("teaching")), f"probe={back}")
            _check("4.11 回到的是同一讲", bool(back and back.get("lessonId") == lesson_id), f"probe={back}")

            # ---- 4.12/4.13 自动翻页后新页完整可见（用户实测：新页上半被字幕条压住一半）----
            #
            # 关键在于**造出「页高 > 容器可视高」**的场景：否则容器根本不滚动，
            # 顶对齐与旧的底对齐观测值完全相同，断言没有区分度（已实测确认过）。
            # 做法是把滚动容器临时压到 180px —— 走的仍是真实滚动逻辑，不动任何数据。
            await ev("""(function(){var b=document.querySelector('.lesson-head');
                if(!b) return false; b.style.flex='none'; b.style.height='120px'; return true;})()""")
            await _asyncio.sleep(0.3)
            # 先把容器拉到底，模拟「用户没主动往下滚、视图停在下方」
            await ev("(function(){var b=document.querySelector('.lesson-head');"
                     "if(b){b.scrollTop=b.scrollHeight;}return true;})()")
            await _asyncio.sleep(0.3)
            nav = await ev("window.__lessonScrollTo ? window.__lessonScrollTo(1) : false")
            await _asyncio.sleep(1.6)
            pos = await ev("""(function(){
                var b=document.querySelector('.lesson-head');
                if(!b) return {err:'no-scroll-box'};
                var cur=document.querySelector('.board-card.slide.current');
                if(!cur) return {err:'no-current'};
                var r=cur.getBoundingClientRect(), br=b.getBoundingClientRect();
                var bar=b.querySelector('.lesson-bar');
                var barH=bar?bar.getBoundingClientRect().height:0;
                return {curTop:Math.round(r.top), boxTop:Math.round(br.top),
                        barH:Math.round(barH), curH:Math.round(r.height),
                        boxH:Math.round(br.height), scrollTop:Math.round(b.scrollTop),
                        scrollable:b.scrollHeight>b.clientHeight};
            })()""")
            _check("4.12 翻页后当前页顶部未被滚出容器",
                   bool(pos and pos.get("curTop") is not None
                        and (pos.get("boxH") or 0) <= 140 and pos.get("curH", 0) > (pos.get("boxH") or 0)
                        and pos["curTop"] >= pos["boxTop"] - 2), f"nav={nav} pos={pos}")
            _check("4.13 翻页后当前页顶部不被 sticky 操作条遮挡",
                   bool(pos and pos.get("curTop") is not None
                        and pos["curTop"] >= pos["boxTop"] + (pos.get("barH") or 0) - 4),
                   f"pos={pos}")
            # 恢复容器样式，避免影响后续断言
            await ev("""(function(){var b=document.querySelector('.lesson-head');
                if(b){b.style.flex=''; b.style.height='';} return true;})()""")
            await _asyncio.sleep(0.2)

            _task.cancel()
    finally:
        try: proc.terminate()
        except Exception: pass
        try: proc.wait(timeout=8)
        except Exception:
            try: proc.kill()
            except Exception: pass


def cdp_settings_tts(base: str) -> None:
    """用 CDP 真实打开设置页，量取语音区关键宽度，证伪「被挤扁」（问题 1）。

    单脚本一次跑完：本函数内启动 Chrome、驱动交互、最后回收进程。
    """
    import asyncio as _a, json, subprocess
    import websockets

    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    ud = "C:/tmp/zhiban_tts_%d" % int(time.time())
    try:
        os.makedirs(ud, exist_ok=True)
    except Exception:
        pass
    port = 9224

    def _check(name, cond, detail=""):
        (PASS if cond else FAIL).append(name)
        print(f"  {'✅' if cond else '❌'} {name}{('  | ' + detail) if (detail and not cond) else ''}")

    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--no-proxy-server", f"--remote-debugging-port={port}",
            f"--user-data-dir={ud}", "--window-size=1280,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ver = None
        for _ in range(60):
            try:
                ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
                break
            except Exception:
                time.sleep(0.5)
        if not ver:
            _check("CDP 端口可达", False, "chrome 未启动")
            return
        _check("CDP 端口可达", True)

        async def _run() -> None:
            async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=None, ping_interval=None) as bws:
                _id = [0]
                def nid():
                    _id[0] += 1
                    return _id[0]
                async def bsend(method, params=None):
                    i = nid()
                    await bws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
                    return i
                async def bwait(i, t=20):
                    while True:
                        r = json.loads(await _a.wait_for(bws.recv(), t))
                        if r.get("id") == i:
                            return r
                r = await bsend("Target.createTarget", {"url": base + "/#/settings"})
                tid = (await bwait(r))["result"]["targetId"]
                pws = None
                for _ in range(40):
                    lst = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json()
                    t = next((x for x in lst if x.get("id") == tid), None)
                    if t and t.get("webSocketDebuggerUrl"):
                        pws = t["webSocketDebuggerUrl"]
                        break
                    await _a.sleep(0.3)
                if not pws:
                    _check("页面调试端点", False)
                    return
                _check("页面调试端点", True)

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
                _task = _a.create_task(reader())
                async def ev(expr, t=25):
                    i = nid()
                    loop = _a.get_event_loop()
                    fut = loop.create_future()
                    pending[i] = fut
                    await ws.send(json.dumps({"id": i, "method": "Runtime.evaluate",
                                              "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
                    try:
                        res = await _a.wait_for(fut, t)
                    finally:
                        pending.pop(i, None)
                    if "error" in res:
                        raise RuntimeError(str(res["error"]))
                    return res.get("result", {}).get("result", {}).get("value")

                await ws.send(json.dumps({"id": nid(), "method": "Runtime.enable"}))
                for _ in range(40):
                    ok = await ev("!!document.getElementById('tts-mode')")
                    if ok:
                        break
                    await _a.sleep(0.3)
                _check("2.60 设置页渲染出 #tts-mode", bool(ok))

                # 默认 off → 云端行 display:none、宽度 0，必须先切到「云端 API」。
                await ev("var m=document.getElementById('tts-mode');m.value='cloud';m.dispatchEvent(new Event('change'));")
                await _a.sleep(0.5)
                w = await ev("""(function(){
                    function w0(el){return el?el.getBoundingClientRect().width:0;}
                    return {base:w0(document.getElementById('tts-base')),
                            model:w0(document.getElementById('tts-model')),
                            mode:w0(document.getElementById('tts-mode')),
                            hasCloud:!!document.getElementById('tts-cloud-row'),
                            hasKey:!!document.getElementById('tts-key-row')};
                })()""")
                _check("2.61 #tts-base 宽度 > 200（未被挤扁）",
                       (w or {}).get("base", 0) > 200, f"base={(w or {}).get('base')}")
                _check("2.62 #tts-model 宽度 > 200（未被挤扁）",
                       (w or {}).get("model", 0) > 200, f"model={(w or {}).get('model')}")
                _check("2.63 #tts-mode 宽度 < 320（已收回自适应宽度）",
                       (w or {}).get("mode", 0) < 320, f"mode={(w or {}).get('mode')}")
                _check("2.64 存在 #tts-cloud-row / #tts-key-row",
                       bool((w or {}).get("hasCloud")) and bool((w or {}).get("hasKey")))

                # 切回 off（等价于 updateTTSVis('off')），断言两行隐藏。
                await ev("var m=document.getElementById('tts-mode');m.value='off';m.dispatchEvent(new Event('change'));")
                await _a.sleep(0.4)
                disp = await ev("""(function(){
                    var cr=document.getElementById('tts-cloud-row');
                    var kr=document.getElementById('tts-key-row');
                    return {cr: cr?cr.style.display:'?', kr: kr?kr.style.display:'?'};
                })()""")
                _check("2.65 切 off 后 #tts-cloud-row 隐藏",
                       (disp or {}).get("cr") == "none", f"cr={(disp or {}).get('cr')}")
                _check("2.66 切 off 后 #tts-key-row 隐藏",
                       (disp or {}).get("kr") == "none", f"kr={(disp or {}).get('kr')}")

                # ── 2.47~2.50：测试连接按钮 + 试听真的发请求 + 暴露实际 URL ──
                _check("2.67 存在 #tts-test 与 #tts-test-result",
                       bool(await ev("!!document.getElementById('tts-test') "
                                     "&& !!document.getElementById('tts-test-result')")))

                live = await ev("document.getElementById('tts-live') "
                                "? document.getElementById('tts-live').textContent : ''")
                _check("2.68 #tts-live 文案含「当前生效」", "当前生效" in (live or ""), f"live={live}")

                # 切到云端，点「🔊 试听」——证明云端试听真的发起了请求，
                # 而不是像以前那样直接 return「设置页不试听」。
                await ev("var m=document.getElementById('tts-mode');"
                         "m.value='cloud';m.dispatchEvent(new Event('change'));")
                await _a.sleep(0.4)
                await ev("var b=document.getElementById('tts-preview');if(b)b.click();")
                # 试听是异步的：先发请求到后端，拿到结果后再异步追加「（实际请求：…）」。
                # 因此必须等到「实际请求」出现，不能一见 HTTP 就收（否则会漏掉追加部分）。
                preview_txt = ""
                for _ in range(60):
                    preview_txt = await ev("document.getElementById('tts-preview-result') "
                                           "? document.getElementById('tts-preview-result').textContent : ''")
                    if preview_txt and "实际请求" in preview_txt:
                        break
                    await _a.sleep(0.3)
                _check("2.69 云端试听发起请求（结果含 HTTP）",
                       "HTTP" in (preview_txt or ""), f"preview={preview_txt}")
                _check("2.70 试听暴露「实际请求」URL",
                       "实际请求" in (preview_txt or ""), f"preview={preview_txt}")

                # ── 2.51~2.52：新增「音色」输入可见 + 可提交 ──
                # 确保处于云端模式（2.49/2.50 已切到 cloud，这里再确保一次）。
                await ev("var m=document.getElementById('tts-mode');"
                         "if(m.value!=='cloud'){m.value='cloud';m.dispatchEvent(new Event('change'));}")
                await _a.sleep(0.4)
                vo = await ev("""(function(){
                    var el=document.getElementById('tts-voice');
                    var row=document.getElementById('tts-cloud-row');
                    return {exists: !!el,
                            w: el ? el.getBoundingClientRect().width : 0,
                            rowVisible: row ? row.style.display !== 'none' : false};
                })()""")
                _check("2.71 存在 #tts-voice 且切到云端后可见（宽度>100）",
                       bool((vo or {}).get("exists")) and (vo or {}).get("w", 0) > 100
                       and bool((vo or {}).get("rowVisible")),
                       f"vo={vo}")

                # 填入一个音色值 → 点「保存语音设置」→ GET /api/settings 回显一致
                TEST_VOICE = "test_voice_xiaxia"
                await ev(f"document.getElementById('tts-voice').value={json.dumps(TEST_VOICE)};")
                await ev("var b=document.getElementById('tts-save');if(b)b.click();")
                saved = None
                for _ in range(40):
                    try:
                        with httpx.Client(trust_env=False) as _c:
                            saved = _c.get(f"{base}/api/settings", timeout=5).json()
                        if (saved.get("data") or {}).get("tts", {}).get("voice") == TEST_VOICE:
                            break
                    except Exception:
                        pass
                    await _a.sleep(0.3)
                _check("2.72 #tts-voice 提交后 GET /api/settings 回显一致",
                       (saved or {}).get("data", {}).get("tts", {}).get("voice") == TEST_VOICE,
                       f"voice={(saved or {}).get('data', {}).get('tts', {}).get('voice')}")

                # 结果文案很长（含 HTTP 状态与实际请求 URL），若与按钮同在一行会把按钮
                # 挤到换行（「保存语音设/置」断词）。断言按钮行只有按钮、且按钮不换行。
                lay = await ev("""(function(){
                  var s=document.getElementById('tts-save');
                  if(!s) return null;
                  var row=s.parentElement;
                  return {rowKids: row.children.length,
                          testInRow: !!row.querySelector('#tts-test-result'),
                          ws: getComputedStyle(s).whiteSpace,
                          h: Math.round(s.getBoundingClientRect().height)};
                })()""")
                _check("2.73 语音按钮独占一行（结果文案不在按钮行内）",
                       bool(lay) and lay.get("rowKids") == 3 and not lay.get("testInRow"),
                       f"lay={lay}")
                _check("2.74 语音按钮不换行（nowrap 且单行高度）",
                       bool(lay) and lay.get("ws") == "nowrap" and 0 < (lay.get("h") or 0) < 46,
                       f"lay={lay}")

                # P1：音色/模型自动发现（docs/07）
                disc = await ev("""(function(){
                  return {dl: !!document.getElementById('tts-voice-list'),
                          mdl: !!document.getElementById('tts-model-list'),
                          fetchBtn: !!document.getElementById('tts-voices-fetch'),
                          probeBtn: !!document.getElementById('tts-voices-probe'),
                          modelBtn: !!document.getElementById('tts-models'),
                          meta: !!document.getElementById('tts-voice-meta'),
                          listInput: (document.getElementById('tts-voice')||{}).getAttribute &&
                                     document.getElementById('tts-voice').getAttribute('list') || ""};
                })()""")
                _check("2.75 音色/模型自动发现的 UI 元素齐备",
                       bool(disc) and all(disc.get(k) for k in
                                          ("dl", "mdl", "fetchBtn", "probeBtn", "modelBtn", "meta")),
                       f"disc={disc}")
                # 不能用 <datalist>：浏览器会按输入框已有值过滤选项
                # （实测模型名 9 个只剩 1 个、音色 8 个一个都不显示），必须用点击填入列表
                _check("2.76 音色/模型走「点击填入」列表（不再用 datalist）",
                       bool(disc) and not disc.get("listInput")
                       and disc.get("dl") is True and disc.get("mdl") is True,
                       f"disc={disc}")

                _task.cancel()

        _a.run(_run())
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


def cdp_visuals(base: str) -> None:
    """CDP 验证课件可视化渲染链路（P1 mermaid / P2 echarts / P3 markmap 依赖）。

    不依赖课程数据：任意页面（vendor 是全局脚本）上直接构造容器调 window.Viz.render，
    断言返回值与 DOM。真实课程数据下的净化/落库由 course_check 的 D27/D28 覆盖。
    """
    import asyncio as _a, json, subprocess
    import websockets

    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    ud = "C:/tmp/zhiban_viz_%d" % int(time.time())
    try:
        os.makedirs(ud, exist_ok=True)
    except Exception:
        pass
    port = 9225

    def _check(name, cond, detail=""):
        (PASS if cond else FAIL).append(name)
        print(f"  {'✅' if cond else '❌'} {name}{('  | ' + detail) if (detail and not cond) else ''}")

    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--no-proxy-server", f"--remote-debugging-port={port}",
            f"--user-data-dir={ud}", "--window-size=1280,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ver = None
        for _ in range(60):
            try:
                ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
                break
            except Exception:
                time.sleep(0.5)
        if not ver:
            _check("viz CDP 端口可达", False, "chrome 未启动")
            return
        _check("viz CDP 端口可达", True)

        async def _run() -> None:
            async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=None, ping_interval=None) as bws:
                _id = [0]
                def nid():
                    _id[0] += 1
                    return _id[0]
                async def bsend(method, params=None):
                    i = nid()
                    await bws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
                    return i
                async def bwait(i, t=20):
                    while True:
                        r = json.loads(await _a.wait_for(bws.recv(), t))
                        if r.get("id") == i:
                            return r
                r = await bsend("Target.createTarget", {"url": base + "/#/settings"})
                tid = (await bwait(r))["result"]["targetId"]
                pws = None
                for _ in range(40):
                    lst = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json()
                    t = next((x for x in lst if x.get("id") == tid), None)
                    if t and t.get("webSocketDebuggerUrl"):
                        pws = t["webSocketDebuggerUrl"]
                        break
                    await _a.sleep(0.3)
                if not pws:
                    _check("viz 页面调试端点", False)
                    return
                _check("viz 页面调试端点", True)

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
                            fut = pending.pop(msg["id"])
                            if not fut.done():
                                fut.set_result(msg)
                _task = _a.ensure_future(reader())
                async def ev(expr, t=25):
                    i = nid()
                    loop = _a.get_event_loop()
                    fut = loop.create_future()
                    pending[i] = fut
                    await ws.send(json.dumps({"id": i, "method": "Runtime.evaluate",
                                              "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
                    try:
                        res = await _a.wait_for(fut, t)
                    finally:
                        pending.pop(i, None)
                    if "error" in res:
                        raise RuntimeError(str(res["error"]))
                    return res.get("result", {}).get("result", {}).get("value")

                await ws.send(json.dumps({"id": nid(), "method": "Runtime.enable"}))
                for _ in range(40):
                    ok = await ev("!!window.Viz")
                    if ok:
                        break
                    await _a.sleep(0.3)
                _check("3.2a Viz 全局可用", bool(ok))
                _check("3.2b mermaid 全局可用", bool(await ev("typeof window.mermaid !== 'undefined'")))
                _check("3.2c echarts 全局可用", bool(await ev("typeof window.echarts !== 'undefined'")))
                _check("3.2d MD.mindmap 可用（P3 导览依赖）",
                       bool(await ev("!!(window.MD && typeof window.MD.mindmap === 'function')")))

                res = await ev("""
                (async () => {
                  const out = {};
                  const mk = (id) => { const d = document.createElement('div');
                                       d.className = 'viz-box'; d.id = id;
                                       document.body.appendChild(d); return d; };
                  out.goodM = await window.Viz.render(mk('v-good-m'),
                    {kind:'diagram', title:'t', bullets:['x'],
                     diagram:{lang:'mermaid', code:'flowchart TD\\n  A[开始] --> B[结束]'}});
                  out.svgGood = !!document.querySelector('#v-good-m svg');
                  out.badM = await window.Viz.render(mk('v-bad-m'),
                    {kind:'diagram', title:'t', bullets:['x'],
                     diagram:{lang:'mermaid', code:'flowchart TD\\nA[未闭合'}});
                  out.fbBadM = !!document.querySelector('#v-bad-m .viz-fallback');
                  out.goodC = window.Viz.render(mk('v-good-c'),
                    {kind:'chart', title:'t', bullets:['x'],
                     chart:{type:'bar', title:'成绩', unit:'分', categories:['甲','乙'],
                            series:[{name:'数学', data:[88,92]}]}});
                  out.cvGood = !!document.querySelector('#v-good-c canvas');
                  out.badC = window.Viz.render(mk('v-bad-c'),
                    {kind:'chart', title:'t', bullets:['x'],
                     chart:{type:'bar', categories:['甲','乙'], series:[{name:'s', data:['x','y']}]}});
                  out.fbBadC = !!document.querySelector('#v-bad-c .viz-fallback');
                  return out;
                })()""")
                _check("3.3 好 mermaid → svg", res.get("goodM") == "svg" and res.get("svgGood"),
                       json.dumps(res)[:120])
                _check("3.4 坏 mermaid → fallback", res.get("badM") == "fallback" and res.get("fbBadM"),
                       json.dumps(res)[:120])
                _check("3.5 好 chart → canvas", res.get("goodC") == "canvas" and res.get("cvGood"),
                       json.dumps(res)[:120])
                _check("3.6 坏 chart → fallback", res.get("badC") == "fallback" and res.get("fbBadC"),
                       json.dumps(res)[:120])

                _task.cancel()

        _a.run(_run())
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


def cdp_regen(base: str) -> None:
    """CDP 实测「重新生成大纲」按钮的状态同步（用户反馈问题 1）。

    点击后按钮应变为「重新生成中…」，任务完成后必须复位为「重新生成大纲」，
    且 stage 提示清空 —— 不允许出现「按钮停在生成中、状态却已完成」。
    """
    import asyncio as _a, json, subprocess
    import websockets

    chrome = r"C://Program Files//Google//Chrome//Application//chrome.exe"
    ud = "C:/tmp/zhiban_regen_%d" % int(time.time())
    try:
        os.makedirs(ud, exist_ok=True)
    except Exception:
        pass
    port = 9226

    def _check(name, cond, detail=""):
        (PASS if cond else FAIL).append(name)
        print(f"  {'✅' if cond else '❌'} {name}{('  | ' + detail) if (detail and not cond) else ''}")

    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--no-proxy-server", f"--remote-debugging-port={port}",
            f"--user-data-dir={ud}", "--window-size=1280,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ver = None
        for _ in range(60):
            try:
                ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
                break
            except Exception:
                time.sleep(0.5)
        if not ver:
            _check("regen CDP 端口可达", False, "chrome 未启动")
            return
        _check("regen CDP 端口可达", True)

        async def _run() -> None:
            async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=None, ping_interval=None) as bws:
                _id = [0]
                def nid():
                    _id[0] += 1
                    return _id[0]
                async def bsend(method, params=None):
                    i = nid()
                    await bws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
                    return i
                async def bwait(i, t=20):
                    while True:
                        r = json.loads(await _a.wait_for(bws.recv(), t))
                        if r.get("id") == i:
                            return r
                r = await bsend("Target.createTarget", {"url": base + "/#/courses"})
                tid = (await bwait(r))["result"]["targetId"]
                pws = None
                for _ in range(40):
                    lst = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json()
                    t = next((x for x in lst if x.get("id") == tid), None)
                    if t and t.get("webSocketDebuggerUrl"):
                        pws = t["webSocketDebuggerUrl"]
                        break
                    await _a.sleep(0.3)
                if not pws:
                    _check("regen 页面调试端点", False)
                    return
                _check("regen 页面调试端点", True)

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
                            fut = pending.pop(msg["id"])
                            if not fut.done():
                                fut.set_result(msg)
                _task = _a.ensure_future(reader())
                async def ev(expr, t=30):
                    i = nid()
                    loop = _a.get_event_loop()
                    fut = loop.create_future()
                    pending[i] = fut
                    await ws.send(json.dumps({"id": i, "method": "Runtime.evaluate",
                                              "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
                    try:
                        res = await _a.wait_for(fut, t)
                    finally:
                        pending.pop(i, None)
                    if "error" in res:
                        raise RuntimeError(str(res["error"]))
                    return res.get("result", {}).get("result", {}).get("value")

                await ws.send(json.dumps({"id": nid(), "method": "Runtime.enable"}))
                for _ in range(40):
                    ok = await ev("!!document.querySelector('#course-list .item')")
                    if ok:
                        break
                    await _a.sleep(0.3)
                _check("3.7 课程列表渲染", bool(ok))
                # 打开第一个课程详情，点「重新生成大纲」
                await ev("document.querySelector('#course-list .item').click()")
                for _ in range(40):
                    ok = await ev("!!document.getElementById('c-regen')")
                    if ok:
                        break
                    await _a.sleep(0.3)
                _check("3.8 详情页出现重新生成按钮", bool(ok))
                await ev("document.getElementById('c-regen').click()")
                for _ in range(30):
                    txt = await ev("document.getElementById('c-regen').textContent")
                    if txt == "重新生成中…":
                        break
                    await _a.sleep(0.3)
                _check("3.9 点击后按钮进入生成中", txt == "重新生成中…", str(txt))
                # mock 端点几秒内完成；轮询按钮复位（上限 240s）
                final = ""
                for _ in range(480):
                    final = await ev("document.getElementById('c-regen') ? document.getElementById('c-regen').textContent : '(gone)'")
                    if final == "重新生成大纲":
                        break
                    await _a.sleep(0.5)
                _check("3.10 完成后按钮复位（不再停留生成中）", final == "重新生成大纲", f"final={final}")
                stage = await ev("(document.getElementById('c-stage')||{textContent:''}).textContent")
                _check("3.11 stage 提示随完成清空", stage == "", f"stage={stage}")

                _task.cancel()

        _a.run(_run())
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


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)
    env = {**os.environ, "ZHIBAN_DATA_DIR": str(DATA), "ZHIBAN_PORT": "8764",
           "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    procs = [
        subprocess.Popen([str(PY), str(ROOT / "dev" / "mock_llm.py")],
                         env={**env, "ZHIBAN_MOCK_PORT": "8765"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        assert wait_http(f"{MOCK}/health") and wait_http(f"{BASE}/api/health"), "服务未启动"
        print("服务已启动（后端 8764 / mock 8765）\n")

        print("[1] 课程页（空态）")
        dom = dump(f"{BASE}/#/courses")
        check("1.1 视图渲染无异常", 'data-view-error' not in dom, dom[:200])
        check("1.2 渲染出新建入口", "新建课程" in dom or "还没有课程" in dom)
        check("1.3 顶栏含课程导航", ">课程<" in dom)

        print("\n[1.5] 设置页")
        dom = dump(f"{BASE}/#/settings")
        check("1.5a 设置页无渲染异常", 'data-view-error' not in dom, dom[:200])
        check("1.5b 有深度思考开关", "深度思考" in dom)

        print("\n[1.8] 课程创建向导")
        dom = dump(f"{BASE}/#/courses?new=1")
        check("1.8a 向导页无渲染异常", 'data-view-error' not in dom, dom[:200])
        check("1.8b 有课型区（「分析材料并预选」按钮 + 课型卡容器）",
              "分析材料并预选" in dom and "intent-cards" in dom)
        check("1.8c 有自动单元数选项", "自动" in dom and "按材料定" in dom)

        print("\n[2] 造一门课并检查课程页 / 课堂页 / 练习页")
        with httpx.Client(trust_env=False) as cli:
            cli.put(f"{BASE}/api/settings", json={
                "llm": {"base_url": f"{MOCK}/v1", "model": "mock-outline",
                        "api_key": "sk-test-ui"}}, timeout=10)
            # 材料用 PDF：材料标注页与图片题都依赖分页材料（切片带 page_no）。
            pdf = io.BytesIO(make_text_pdf([
                "Chapter 3 Limits and the L Hopital rule",
                "A limit describes the trend of a function near a given point.",
                "The L Hopital rule applies to the 0/0 form and to the infinity",
                "over infinity form. You must verify the indeterminate form",
                "before applying the rule, otherwise the conclusion is wrong.",
                "A function is continuous at a point when the limit equals the",
                "function value. Typical exercises combine limits, continuity",
                "and the rule together, so check conditions before computing.",
            ]))
            r = cli.post(f"{BASE}/api/documents/upload",
                         files={"files": ("calculus.pdf", pdf, "application/pdf")},
                         timeout=30).json()
            doc_id = r["data"]["documents"][0]["id"]
            for _ in range(60):
                d = cli.get(f"{BASE}/api/documents/{doc_id}", timeout=10).json()["data"]["document"]
                if d["status"] in ("ready", "failed"):
                    break
                time.sleep(0.5)
            r = cli.post(f"{BASE}/api/courses", json={
                "goal": "学会洛必达法则", "document_ids": [doc_id], "unit_count": 2},
                timeout=20).json()
            cid = r["data"]["course_id"]
            job_id = r["data"]["job_id"]
            for _ in range(120):
                job = cli.get(f"{BASE}/api/courses/jobs/{job_id}", timeout=10).json()["data"]
                if job["status"] in ("ready", "failed"):
                    break
                time.sleep(0.5)
            course = cli.get(f"{BASE}/api/courses/{cid}", timeout=10).json()["data"]
            lesson_id = course["units"][0]["lessons"][0]["id"]
            # 练习类讲次（mock 大纲里第 1、2 单元各有一节 kind=practice）
            practice_id = next((l["id"] for u in course["units"] for l in u["lessons"]
                                if l.get("kind") == "practice"), None)

        # 进入课堂页后自动触发讲义生成（P2 自动流）。
        # 无头测试中 6 秒虚拟时间可能足够完成生成，因此不再断言具体的
        # 「无讲义」文案，只保留「无渲染异常」与「有生成入口」两项。
        dom = dump(f"{BASE}/#/lessons/{lesson_id}")
        check("2.0a 课堂页无渲染异常", 'data-view-error' not in dom, dom[:300])
        check("2.0b 有生成讲义入口", "生成讲义" in dom)

        with httpx.Client(trust_env=False) as cli:
            cli.put(f"{BASE}/api/settings", json={"llm": {"model": "mock-lecture"}}, timeout=10)
            r = cli.post(f"{BASE}/api/courses/lessons/{lesson_id}/lecture", timeout=20).json()
            for _ in range(120):
                job = cli.get(f"{BASE}/api/courses/jobs/{r['data']['job_id']}",
                              timeout=10).json()["data"]
                if job["status"] in ("ready", "failed"):
                    break
                time.sleep(0.5)

            cli.put(f"{BASE}/api/settings", json={"llm": {"model": "mock-practice"}}, timeout=10)
            r = cli.post(f"{BASE}/api/courses/lessons/{lesson_id}/practice",
                         json={"count": 5}, timeout=20).json()
            for _ in range(120):
                job = cli.get(f"{BASE}/api/courses/jobs/{r['data']['job_id']}",
                              timeout=10).json()["data"]
                if job["status"] in ("ready", "failed"):
                    break
                time.sleep(0.5)

            # 先把第一讲标记完成：用于「接下来」高亮验证（CDP 真实进入课程详情），
            # 随后仍会把剩余讲次标记完成以生成单元总结。
            first_lid = course["units"][0]["lessons"][0]["id"]
            first_title = course["units"][0]["lessons"][0]["title"]
            cli.put(f"{BASE}/api/courses/lessons/{first_lid}", json={"complete": True}, timeout=10)

            # 交互级验证（CDP 真实浏览器：课程详情高亮 + 上课/暂停继续/回到课堂浮动入口）
            print("\n[4] 交互级验证（CDP 真实浏览器）")
            try:
                asyncio.run(cdp_interactive(BASE, lesson_id, first_title))
            except Exception as e:
                check("4.0 CDP 验证脚本未异常", False, str(e)[:200])

            # ---- P1 特色页端到端：后端落库的 table/takeaway → 课堂页真实渲染 ----
            # （后端契约由 course_check D31–D34 保证、渲染函数由 viz_harness 保证，
            #   这里补上两者的连接：renderSlides 真的把它们放到页面上）
            cli.put(f"{BASE}/api/settings", json={"llm": {"model": "mock-lecture-p1"}}, timeout=10)
            last_lec = [l for u in course["units"] for l in u["lessons"]
                        if l.get("kind") == "lecture"][-1]
            jr = cli.post(f"{BASE}/api/courses/lessons/{last_lec['id']}/lecture", timeout=20).json()
            for _ in range(160):
                jb = cli.get(f"{BASE}/api/courses/jobs/{jr['data']['job_id']}", timeout=10).json()
                if jb["data"]["status"] != "running":
                    break
                time.sleep(0.5)
            p1dom = dump(f"{BASE}/#/lessons/{last_lec['id']}")
            check("4.14 对比表格页在课堂真实渲染（.viz-table + 3 列表头）",
                  'class="viz-table"' in p1dom and p1dom.count("<th>") >= 3)
            check("4.15 金句卡在课堂真实渲染（.takeaway-box）",
                  'class="takeaway-box"' in p1dom)
            check("4.16 坏表格未渲染出缺列的表（该页退化为要点页）",
                  "列数不齐的坏表格" not in p1dom
                  or p1dom.count('class="viz-table"') == 1)
            # 提示词引导模型把关键术语/编号用 **加粗** 标出；若用纯文本渲染，
            # 学生看到的是字面星号（实测曾是如此）。
            check("4.17 要点里的 **加粗** 已渲染为 <strong> 而非字面星号",
                  "<strong>" in p1dom and "**936**" not in p1dom)
            # Archify 式图示：后端编译好的 SVG 在课堂页真实渲染
            with httpx.Client(trust_env=False, timeout=20) as c4:
                c4.put(f"{BASE}/api/settings", json={"llm": {"model": "mock-lecture-ir"}})
                _jr = c4.post(f"{BASE}/api/courses/lessons/{last_lec['id']}/lecture",
                              timeout=20).json()
                for _ in range(160):
                    _jb = c4.get(f"{BASE}/api/courses/jobs/{_jr['data']['job_id']}",
                                 timeout=10).json()["data"]
                    if _jb["status"] != "running":
                        break
                    time.sleep(0.5)
                c4.put(f"{BASE}/api/settings", json={"llm": {"model": "mock-lecture"}})
            irdom = dump(f"{BASE}/#/lessons/{last_lec['id']}")
            check("4.18 后端编译的图示在课堂页内联渲染（.zf-svg）",
                  'class="zf-svg"' in irdom and "zf-node" in irdom)
            check("4.19 图上节点文字可见（不是空图）",
                  "打开命令行" in irdom or "运行 chcp" in irdom)
            check("4.20 图示不再依赖 mermaid 运行时（无 mermaid 报错回退文案）",
                  "本页图示未能渲染" not in irdom)
            cli.put(f"{BASE}/api/settings", json={"llm": {"model": "mock-lecture"}}, timeout=10)

            # 完成剩余讲次 → 单元总结可生成（供单元总结页签验证）
            for u in course["units"]:
                for l in u["lessons"]:
                    cli.put(f"{BASE}/api/courses/lessons/{l['id']}",
                            json={"complete": True}, timeout=10)
            unit_id = course["units"][0]["id"]
            cli.put(f"{BASE}/api/settings", json={"llm": {"model": "mock-summary"}}, timeout=10)
            r = cli.post(f"{BASE}/api/courses/units/{unit_id}/summary", timeout=20).json()
            for _ in range(120):
                job = cli.get(f"{BASE}/api/courses/jobs/{r['data']['job_id']}",
                              timeout=10).json()["data"]
                if job["status"] in ("ready", "failed"):
                    break
                time.sleep(0.5)

        dom = dump(f"{BASE}/#/courses")
        check("2.1 课程页无渲染异常", 'data-view-error' not in dom, dom[:300])
        check("2.2 显示课程标题", "极限" in dom or "洛必达" in dom)

        # P1：结构编辑（思维导图 + 编辑器）
        dom = dump(f"{BASE}/#/courses?confirm={cid}")
        check("2.3 结构编辑页无渲染异常", 'data-view-error' not in dom, dom[:300])
        check("2.4 有结构编辑器", "tree-edit" in dom and "unit-box" in dom)
        check("2.5 有思维导图容器", "tree-map" in dom and 'id="map"' in dom)
        check("2.6 导图已渲染 SVG", "<svg" in dom)
        check("2.7 有保存按钮", "保存结构" in dom or "确认，开始学习" in dom)

        dom = dump(f"{BASE}/#/lessons/{lesson_id}")
        check("2.8 课堂页无渲染异常", 'data-view-error' not in dom, dom[:300])
        check("2.8a 有准备好了吗弹窗", "准备好了吗" in dom)
        check("2.8b 有开始上课按钮", "开始上课" in dom)
        # P2 追加：课件 / 讲师讲述 / 语音输入 / 引用来源默认收起
        check("2.8c 有课件页签", "课件" in dom)
        check("2.8d 有讲师讲述区", "讲师讲述" in dom)
        check("2.8d2 讲述区提示可回看历史", "可以往上翻" in dom)
        check("2.8e 有语音输入按钮", "语音" in dom)
        check("2.8f 引用来源默认收起", "展开" in dom and "引用来源" in dom)
        check("2.8g 课件逐页渲染（幻灯片式）", "slide-bar" in dom and "第 1 页" in dom)
        check("2.8h 课件含短要点列表", "<li>" in dom and "极限的直觉" in dom)
        check("2.9 渲染白板", "board-card" in dom or "board-summary" in dom)
        check("2.10 有课堂提问区", "课堂提问" in dom)
        check("2.11 有页签", "材料标注" in dom and "单元总结" in dom)
        check("2.12 有导出与上课入口", "导出图片" in dom and "开始上课" in dom)
        check("2.13 AI 标注已展示", "材料标注" in dom)

        # P1：材料标注页（pdf.js 渲染 + 标注覆盖层）
        dom = dump(f"{BASE}/#/lessons/{lesson_id}?tab=marks")
        check("2.14 材料标注页无渲染异常", 'data-view-error' not in dom, dom[:300])
        check("2.15 有标注工具条", "mk-bar" in dom and "圈注" in dom)
        check("2.16 有渲染画布", "mk-stage" in dom and "<canvas" in dom)
        check("2.17 有旁注面板", "mk-notes" in dom or "旁注" in dom)
        check("2.18 无渲染失败提示", "渲染失败" not in dom, dom[:400])

        # P1：单元总结页
        dom = dump(f"{BASE}/#/lessons/{lesson_id}?tab=summary")
        check("2.19 单元总结页无渲染异常", 'data-view-error' not in dom, dom[:300])
        check("2.20 显示总结状态", "单元" in dom and ("已生成" in dom or "生成单元总结" in dom))
        check("2.21 显示待巩固/已掌握", "待巩固" in dom or "已掌握" in dom)

        # 练习类讲次：进去不能是「一片空白」（用户反馈：第 3 课点进去什么都没有）
        if practice_id:
            pdom = dump(f"{BASE}/#/lessons/{practice_id}")
            _m = re.search(r'id="lesson-tabs"[^>]*>(.*?)</div>', pdom, re.S)
            _tabs_html = _m.group(1) if _m else ""
            _labels = re.findall(r'>([^<>]+)</button>', _tabs_html)
            _active = re.findall(r'<button[^>]*class="active"[^>]*>([^<>]+)</button>', _tabs_html)
            check("2.21a 练习讲次页签只有「练习 / 单元总结」",
                  _labels == ["练习", "单元总结"], f"labels={_labels}")
            check("2.21b 练习讲次默认落在「练习」页签",
                  _active == ["练习"], f"active={_active}")
            # 用卡片自身的标题文案判定，别用「随堂练习」这种满页都有的词
            # （顶栏按钮「生成随堂练习」也含它，会假通过）
            check("2.21c 练习讲次给出引导卡片而非空白",
                  "这是一节随堂练习" in pdom,
                  "未找到引导卡片标题")
            check("2.21d 练习讲次不再出现孤立空提示",
                  "这一节还没有课件内容。" not in pdom)
            with httpx.Client(trust_env=False) as _cl:
                _pfull = _cl.get(f"{BASE}/api/courses/lessons/{practice_id}",
                                 timeout=10).json()["data"]
            _p_slides = len(_pfull.get("slides") or [])
            # 不变式：有课件才该出现「开始上课 / 导出图片 / 导出讲义」
            # 只认按钮 id：「开始上课 / 导出图片」这些字在提示文案里也出现，用文本判定会假通过
            _shown = ('id="b-speak"' in pdom, 'id="b-png"' in pdom, 'id="b-md"' in pdom)
            check("2.21e 依赖课件的按钮与「有无课件」一致",
                  all(_shown) if _p_slides > 0 else not any(_shown),
                  f"slides={_p_slides} shown={_shown}")
            # 回归：普通讲次仍是四个页签
            _ldom = dump(f"{BASE}/#/lessons/{lesson_id}")
            _lm = re.search(r'id="lesson-tabs"[^>]*>(.*?)</div>', _ldom, re.S)
            _llabels = re.findall(r'>([^<>]+)</button>', _lm.group(1) if _lm else "")
            check("2.21f 普通讲次仍是四个页签",
                  _llabels == ["课件", "讲义", "材料标注", "单元总结"], f"labels={_llabels}")
        else:
            check("2.21a 找到练习类讲次", False, "mock 课程里没有 kind=practice 的讲次")

        # P1：图片题（pdf.js 渲染材料页）
        dom = dump(f"{BASE}/#/practice/{lesson_id}")
        check("2.22 练习页无渲染异常", 'data-view-error' not in dom, dom[:300])
        check("2.23 渲染题干", "随堂练习" in dom)
        check("2.24 渲染选项或输入框", "opt-row" in dom or "fill-input" in dom)
        # 未作答不允许进入下一题（用户反馈：以前没选也能点「下一步」）
        check("2.25 有下一题按钮", 'id="q-next"' in dom)
        check("2.26 未作答时下一题被禁用",
              re.search(r'id="q-next"[^>]*\bdisabled\b', dom) is not None,
              (re.search(r'<button[^>]*id="q-next"[^>]*>', dom) or [""])[0]
              if re.search(r'<button[^>]*id="q-next"[^>]*>', dom) else "未找到按钮")
        check("2.27 给出未作答提示", "未作答时无法进入下一题" in dom)

        # C 批：hands_on 回填式实操题的渲染（重新出题换成含 hands_on 的桩，
        # 桩把 hands_on 放在第一题，无状态 dump 才能看到徽章）。
        # 注意此处已在 with httpx.Client 块之外，必须另开 client。
        with httpx.Client(trust_env=False, timeout=20) as c2:
            c2.put(f"{BASE}/api/settings", json={"llm": {"model": "mock-practice-hands"}})
            r = c2.post(f"{BASE}/api/courses/lessons/{lesson_id}/practice",
                        json={"count": 5}).json()
            for _ in range(160):
                job = c2.get(f"{BASE}/api/courses/jobs/{r['data']['job_id']}").json()["data"]
                if job["status"] in ("ready", "failed"):
                    break
                time.sleep(0.5)
        hdom = dump(f"{BASE}/#/practice/{lesson_id}")
        check("2.27a 实操题渲染「🖐 实操题」徽章",
              "hands-on-badge" in hdom and "实操题" in hdom)
        check("2.27b 实操题题干与回填提示就位",
              "chcp" in hdom and "把你实际操作看到的结果填进来" in hdom)
        check("2.27c 实操题用单行输入作答（fill-input）", "fill-input" in hdom)
        with httpx.Client(trust_env=False, timeout=20) as c3:
            c3.put(f"{BASE}/api/settings", json={"llm": {"model": "mock-practice"}})

        # 授课舞台：屏幕中下方字幕（暂停式互动检查点已按用户要求永久移除）
        dom_lesson = dump(f"{BASE}/#/lessons/{lesson_id}")
        check("2.28 授课舞台容器存在", 'id="teach-stage"' in dom_lesson)
        check("2.29 字幕行存在", 'id="teach-sub"' in dom_lesson)
        check("2.30 暂停式互动已彻底移除",
              "听到这里" not in dom_lesson and 'id="teach-ask"' not in dom_lesson
              and "继续上课" not in dom_lesson and "重讲本页" not in dom_lesson
              and "我有疑问" not in dom_lesson and "不用停" not in dom_lesson)
        check("2.31 未上课时舞台隐藏",
              re.search(r'id="teach-stage"[^>]*\bhidden\b', dom_lesson) is not None,
              (re.search(r'<div[^>]*id="teach-stage"[^>]*>', dom_lesson) or [""])[0]
              if re.search(r'<div[^>]*id="teach-stage"[^>]*>', dom_lesson) else "未找到")
        check("2.32 上课弹窗说明字幕", "屏幕中下方会同步显示字幕" in dom_lesson)
        check("2.33 弹窗说明讲完自动测验", "讲完自动进入随堂测验" in dom_lesson)

        # 顶部操作条固定 / 左上角位置标签 / 课件居中补白列（课堂页体验修改）
        pos_m = re.search(r'id="lesson-pos"[^>]*>(.*?)</span>', dom_lesson, re.S)
        pos_text = pos_m.group(1) if pos_m else ""
        check("2.34 课堂页含 lesson-bar 顶部操作条", "lesson-bar" in dom_lesson)
        check("2.35 课堂页含 lesson-pos 位置标签", "lesson-pos" in dom_lesson)
        check("2.36 lesson-pos 显示「第X单元·第Y课」",
              re.search(r'第 \d+ 单元 · 第 \d+ 课', pos_text) is not None, pos_text)
        check("2.37 课堂页含左补白列 col-spacer", "col-spacer" in dom_lesson)
        check("2.38 返回按钮在动作组之前",
              dom_lesson.index('id="b-back"') < dom_lesson.index('id="lesson-actions"'))
        check("2.39 仅存在一个返回按钮", dom_lesson.count('id="b-back"') == 1,
              f"count={dom_lesson.count('id=\"b-back\"')}")

        # 设置页语音区布局（问题 1 核心：#tts-base / #tts-model 不能被挤扁）
        print("\n[2.60] 设置页语音区布局（CDP 真实浏览器）")
        try:
            cdp_settings_tts(BASE)
        except Exception as e:
            check("2.99 CDP 语音布局验证未异常", False, str(e)[:200])

        # 课件可视化渲染链路（P1 mermaid / P2 echarts / P3 markmap）
        print("\n[3.2] 课件可视化渲染（CDP 真实浏览器）")
        try:
            cdp_visuals(BASE)
        except Exception as e:
            check("3.2z CDP 可视化验证未异常", False, str(e)[:200])

        # 「重新生成大纲」按钮状态同步（用户反馈问题 1）
        print("\n[3.7] 重新生成大纲按钮状态（CDP 真实浏览器）")
        try:
            cdp_regen(BASE)
        except Exception as e:
            check("3.12 CDP 重新生成验证未异常", False, str(e)[:200])

        # 页面级 JS 错误会写进 DOM（main.js 的 catch 分支）
        m = re.search(r'data-view-error="1"[^>]*>加载失败：([^<]{0,140})', dom)
        check("3.1 练习页无 JS 异常", m is None, m.group(1) if m else "")

    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
