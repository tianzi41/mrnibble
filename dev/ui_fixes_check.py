"""四处前端改动的端到端验证（真实无头 Chrome + mock LLM，独立于 course_ui_check）。

覆盖用户 2026-09-19 反馈的四条：
  [1] 材料生成状态 #t-status 移到「② 确认目录，开始写正文」右侧，且**真实可见**
      （display / 字号 / 字重 / 与按钮同排同侧几何），写材料时 writing、写完 done。
  [2] 选课型后 #f-goal 出现 loading 态，响应回来必须退掉（MutationObserver 抓瞬态，
      任何分支都不许留永久 loading）。
  [3] 「AI 材料」区块的说明文字已删（且区块本身仍正常渲染）。
  [4] 结构编辑页单元数量可控：请求体带 / 不带 unit_count；并真端到端证明
      「note 里写『五章』真能出 5 个单元」（判别性用例：课程原值=2，若解析失效只会得 2）。

为什么要它：course_ui_check 是 DOM 冒烟，看不出「状态写进了隐藏容器」「loading 卡死」
这类**可见性/时序**缺陷。本脚本用 CDP 真实交互 + 几何/样式断言补上这一层。

用法::
    .venv/Scripts/python.exe dev/ui_fixes_check.py
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from course_ui_check import dump, ui_profile  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".tmp" / ("test-data-uifix-%d" % int(time.time()))
BASE = "http://127.0.0.1:8770"
MOCK = "http://127.0.0.1:8771"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = 9228

GOAL_PLACEHOLDER = "选好课型后会自动写一句；也可以自己改，或清空让系统按课型决定"
GOAL_WRITING = "正在按课型写目标…"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK' if cond else 'XX'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


def api(method: str, path: str, **kw):
    with httpx.Client(trust_env=False) as cli:
        r = cli.request(method, BASE + path, timeout=kw.pop("timeout", 60), **kw)
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text[:300], "_status": r.status_code}


def payload(resp):
    return resp.get("data") if isinstance(resp, dict) and "data" in resp else resp


# ── 服务 ────────────────────────────────────────────────────────
def start_services():
    DATA.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ, "MRNIBBLE_DATA_DIR": str(DATA), "MRNIBBLE_PORT": "8770",
        "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
    }
    return [
        subprocess.Popen([str(PY), str(ROOT / "dev" / "mock_llm.py")],
                         env={**env, "MRNIBBLE_MOCK_PORT": "8771"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]


def wait_http(url: str, timeout: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with httpx.Client(trust_env=False) as cli:
                if cli.get(url, timeout=3).status_code < 500:
                    return True
        except Exception:
            time.sleep(0.6)
    return False


def use_model(name: str) -> None:
    api("PUT", "/api/settings", json={"llm": {
        "base_url": f"{MOCK}/v1", "model": name, "api_key": "sk-test-uifix"}})


def wait_gen(gid: str, timeout: float = 120.0) -> dict:
    for _ in range(int(timeout * 2)):
        d = payload(api("GET", f"/api/generations/{gid}"))
        if d and d.get("status") in ("ready", "failed", "error"):
            return d
        time.sleep(0.5)
    return {"status": "timeout"}


def wait_job(job_id: str, timeout: float = 120.0) -> dict:
    for _ in range(int(timeout * 2)):
        d = payload(api("GET", f"/api/courses/jobs/{job_id}"))
        if d and d.get("status") in ("ready", "failed"):
            return d
        time.sleep(0.5)
    return {"status": "timeout"}


def wait_course_ready(cid: str, timeout: float = 120.0) -> dict:
    """等到课程 status 落定，返回**课程对象本身**。

    ⚠️ `GET /api/courses/{cid}` 的 `data` 就是课程对象（`_course_out` 已摊平，
    units/status 都在顶层），没有 `data.course` 这层包装 —— 早先按 `d["course"]`
    取会永远拿到 None，症状是"实得 0 个单元"这种假失败。
    """
    for _ in range(int(timeout * 2)):
        c = payload(api("GET", f"/api/courses/{cid}"))
        if isinstance(c, dict) and c.get("status") in ("ready", "failed"):
            return c
        time.sleep(0.5)
    return {}


def build_material() -> str:
    """跑完「生成目录 → 写正文」，返回入库的 doc_id（课程页「AI 材料」区块靠它显示）。"""
    gid = payload(api("POST", "/api/materials/outline",
                      json={"topic": "装饰器入门", "depth": "brief"}))["generation_id"]
    gen = wait_gen(gid)
    chapters = ((gen.get("content_json") or {}).get("outline") or {}).get("chapters") or []
    api("POST", "/api/materials/chapters", json={"generation_id": gid, "chapters": chapters})
    gen = wait_gen(gid)
    return ((gen.get("content_json") or {}).get("doc_id")) or ""


def new_course(doc_id: str, unit_count: int, goal: str) -> str:
    r = payload(api("POST", "/api/courses", json={
        "goal": goal, "document_ids": [doc_id], "unit_count": unit_count}))
    wait_job(r["job_id"])
    wait_course_ready(r["course_id"])
    return r["course_id"]


# ── CDP ─────────────────────────────────────────────────────────
async def with_page(url: str, fn) -> None:
    import websockets

    ud = ui_profile("uifix")
    try:
        os.makedirs(ud, exist_ok=True)
    except Exception:
        pass
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-proxy-server",
         f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={ud}",
         "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(60):
            try:
                lst = httpx.get(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2).json()
                t = next((x for x in lst if x.get("type") == "page"), None)
                if t:
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ws_url:
            check("CDP 端口可达", False, "chrome 未启动")
            return

        async with websockets.connect(ws_url, max_size=None, ping_interval=None) as ws:
            _id = [0]
            pending: dict = {}

            def nid():
                _id[0] += 1
                return _id[0]

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
                        f = pending.pop(msg["id"])
                        if not f.done():
                            f.set_result(msg)

            task = asyncio.ensure_future(reader())

            async def ev(expr: str, t: float = 40):
                i = nid()
                fut = asyncio.get_event_loop().create_future()
                pending[i] = fut
                await ws.send(json.dumps({"id": i, "method": "Runtime.evaluate", "params": {
                    "expression": expr, "returnByValue": True, "awaitPromise": True}}))
                try:
                    res = await asyncio.wait_for(fut, t)
                finally:
                    pending.pop(i, None)
                if "error" in res:
                    raise RuntimeError(str(res["error"])[:300])
                r = res.get("result", {})
                if r.get("exceptionDetails"):
                    raise RuntimeError(str(r["exceptionDetails"])[:300])
                return r.get("result", {}).get("value")

            await ws.send(json.dumps({"id": nid(), "method": "Runtime.enable"}))
            await ws.send(json.dumps({"id": nid(), "method": "Page.enable"}))
            await ws.send(json.dumps({"id": nid(), "method": "Page.navigate",
                                      "params": {"url": url}}))
            for _ in range(80):
                try:
                    if await ev("document.readyState === 'complete'"):
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            try:
                await fn(ev)
            finally:
                task.cancel()
    finally:
        proc.terminate()


PROBE = """(() => {
  const s = document.getElementById('t-status');
  if (!s) return null;
  const b = document.getElementById('t-write');
  const cs = getComputedStyle(s);
  const sr = s.getBoundingClientRect();
  const br = b ? b.getBoundingClientRect() : {top: -999, right: -999};
  return {cls: s.className, txt: s.textContent, disp: cs.display, fs: cs.fontSize,
          fw: cs.fontWeight, color: cs.color, w: Math.round(sr.width),
          sameRow: Math.abs(sr.top - br.top) < 24, rightOf: sr.left >= br.right - 2};
})()"""

GOAL_PROBE = """(() => {
  const g = document.getElementById('f-goal');
  if (!g) return null;
  return {cls: g.className, ph: g.placeholder, val: g.value,
          bc: getComputedStyle(g).borderColor, sh: getComputedStyle(g).boxShadow};
})()"""


async def fn_wizard(ev) -> None:
    for _ in range(60):
        if await ev("!!document.getElementById('f-topic')"):
            break
        await asyncio.sleep(0.3)

    # [2] 埋探针：抓 #f-goal 的 class / placeholder 瞬态；
    #      同时记录 suggest-goal 请求条数（验证「材料没写好时不发请求」）。
    await ev("""(() => {
      window.__goalLog = [];
      window.__goalPosts = [];
      const g = document.getElementById('f-goal');
      const rec = () => window.__goalLog.push({cls: g.className, ph: g.placeholder});
      rec();
      new MutationObserver(rec).observe(g, {attributes: true,
        attributeFilter: ['class', 'placeholder']});
      const _p = Api.post.bind(Api);
      Api.post = (u, b) => {
        if (String(u).indexOf('suggest-goal') >= 0) window.__goalPosts.push(String(u));
        return _p(u, b);
      };
      return true;
    })()""")

    await ev("document.getElementById('src-topic').click()")
    await ev("document.getElementById('f-topic').value='装饰器入门'")
    for _ in range(40):
        if await ev("!!document.querySelector('.intent-card')"):
            break
        await asyncio.sleep(0.3)

    # [2a] 主题模式下材料还没写好：点课型卡**不发请求**（空 ids 会被后端兜底成
    # 「全部已解析材料」→ 目标按库里无关旧材料生成，正是用户实测的 bug），
    # 只提示「材料写好后…」。同步守卫没有让出线程，也就不该有 loading 闪态。
    await ev("document.querySelector('.intent-card').click()")
    await asyncio.sleep(0.5)
    hint = await ev("(document.getElementById('f-goal-hint')||{}).textContent || ''")
    posts_a = await ev("window.__goalPosts.length") or 0
    log_a = json.loads(await ev("JSON.stringify(window.__goalLog)") or "[]")
    print("\n[2a] 材料未就绪时点课型（守卫：不发请求）")
    check("[2a] 提示「材料写好后，会按所选课型自动写目标」且未发请求",
          "材料写好后" in (hint or "") and posts_a == 0, f"hint={hint!r} posts={posts_a}")
    check("[2a] 同步守卫路径不出现 loading 闪态（无可生成内容）",
          not any("loading" in (e.get("cls") or "") for e in log_a), str(log_a)[:200])

    # [1] 材料生成状态：点①出目录 → 点②写正文
    await ev("document.getElementById('t-outline').click()")
    shown = False
    for _ in range(120):
        if await ev("document.getElementById('t-outline-wrap').style.display !== 'none'"):
            shown = True
            break
        await asyncio.sleep(0.5)
    print("\n[1] 材料生成状态 #t-status（按钮右侧 · 可见性）")
    check("[1] 目录已生成（#t-outline-wrap 显示）", shown)

    await ev("document.getElementById('t-write').click()")
    writing = None
    for _ in range(60):
        st = await ev(PROBE)
        if st and "writing" in (st.get("cls") or ""):
            writing = st
            break
        await asyncio.sleep(0.3)
    check("[1] 写材料时 #t-status 进入 writing 态", bool(writing), str(writing))
    if writing:
        check("[1] 文案是「正在写材料 n/N 章…（可切到别的页，回来自动继续）」",
              "正在写材料" in (writing.get("txt") or "")
              and "可切到别的页" in (writing.get("txt") or ""), str(writing))
        check("[1] #t-status 真实可见（display≠none / 有宽度）",
              writing.get("disp") not in ("none", None) and (writing.get("w") or 0) > 40,
              str(writing))
        check("[1] #t-status 与 #t-write 同一行、且在其右侧",
              bool(writing.get("sameRow")) and bool(writing.get("rightOf")), str(writing))
        check("[1] 有视觉权重（字号≥13 / 字重≥600）",
              float(str(writing.get("fs", "0")).replace("px", "") or 0) >= 13
              and int(str(writing.get("fw", "400"))) >= 600, str(writing))

    done = None
    for _ in range(160):
        st = await ev(PROBE)
        if st and ("done" in (st.get("cls") or "") or "warn" in (st.get("cls") or "")):
            done = st
            break
        await asyncio.sleep(0.5)
    check("[1] 材料写完 → #t-status 进入 done 态且含「材料已就绪」",
          bool(done) and "材料已就绪" in (done.get("txt") or "") and "done" in (done.get("cls") or ""),
          str(done))

    # [2b] 材料就绪 → 已选课型 → 自动补目标（finishWriting 补触发，先选课型
    # 再写材料的顺序也覆盖到）；然后再点一次课型卡：这次真的发请求，
    # loading 瞬态必须出现并退掉（[2] 原有的瞬态覆盖搬到这里 ——
    # 场景从「发错请求」换成「材料就绪后正确发请求」）。
    auto_goal = ""
    for _ in range(40):
        auto_goal = await ev("(document.getElementById('f-goal')||{}).value || ''") or ""
        if auto_goal.strip():
            break
        await asyncio.sleep(0.3)
    check("[2b0] 材料就绪后按已选课型自动补上目标（无需再手点）",
          bool(auto_goal.strip()), str(auto_goal)[:100])
    await ev("document.getElementById('f-goal').value = ''")   # 恢复「空框」前提
    log_before = len(json.loads(await ev("JSON.stringify(window.__goalLog)") or "[]"))
    await ev("document.querySelector('.intent-card').click()")
    await asyncio.sleep(0.25)
    goal_mid = await ev(GOAL_PROBE)          # 尽量抓在途中
    await asyncio.sleep(4)
    goal_after = await ev(GOAL_PROBE)
    goal_log = json.loads(await ev("JSON.stringify(window.__goalLog)") or "[]")
    fresh = goal_log[log_before:]

    seen_loading = any("loading" in (e.get("cls") or "") for e in fresh) \
        or "loading" in ((goal_mid or {}).get("cls") or "")
    seen_writing_ph = any((e.get("ph") or "") == GOAL_WRITING for e in fresh) \
        or (goal_mid or {}).get("ph") == GOAL_WRITING
    end_clean = "loading" not in ((goal_after or {}).get("cls") or "")
    ph_restored = (goal_after or {}).get("ph") == GOAL_PLACEHOLDER

    print("\n[2b] 选课型 → 学习目标框的「正在生成」态（材料就绪后）")
    check("[2b] 点课型卡后 #f-goal 出现过 loading 态", seen_loading, f"log={fresh}")
    check("[2b] 空框时 placeholder 出现「正在按课型写目标…」", seen_writing_ph, str(goal_mid))
    check("[2b] 请求结束后 loading 必退（不留永久 loading）", end_clean, str(goal_after))
    check("[2b] placeholder 已还原", ph_restored, str(goal_after))


EDITOR_PROBE = """(() => {
  const s = document.getElementById('o-units');
  if (!s) return null;
  return {val: s.value, txt: s.options[s.selectedIndex].textContent,
          nOpts: s.options.length, hasCustom: !!document.getElementById('o-units-custom'),
          customShown: (document.getElementById('o-units-custom') || {}).style
            ? document.getElementById('o-units-custom').style.display !== 'none' : null};
})()"""

HOOK_POSTS = """(() => {
  const A = window.Api;
  if (!A) return 'NO_API';
  window.__posts = [];
  const _p = A.post.bind(A);
  A.post = (u, b) => {
    if (String(u).includes('outline:regenerate')) {
      window.__posts.push({u: String(u), b: JSON.parse(JSON.stringify(b || {}))});
    }
    return _p(u, b);
  };
  return 'OK';
})()"""


async def fn_editor(ev, cid: str, expect_units: int, pick: str) -> None:
    """pick: "5" = 选 5 个单元；"keep" = 保持当前。"""
    for _ in range(80):
        if await ev("!!document.getElementById('o-units')"):
            break
        await asyncio.sleep(0.3)
    info = await ev(EDITOR_PROBE)
    tag = "[4] 结构编辑页 · 单元数量"
    print(f"\n{tag}（课程原值 {expect_units} 个单元；本次选择：{pick}）")
    check(f"{tag} 有 #o-units 下拉 + 自定义输入", bool(info and info.get("hasCustom")), str(info))
    check(f"{tag} 默认选中「保持当前（{expect_units} 个单元）」",
          bool(info) and info.get("val") == ""
          and f"保持当前（{expect_units} 个单元）" in (info.get("txt") or ""), str(info))

    hooked = await ev(HOOK_POSTS)
    check(f"{tag} 已装上请求探针", hooked == "OK", str(hooked))

    if pick == "keep":
        await ev("document.getElementById('o-units').value = ''")
    else:
        await ev(f"document.getElementById('o-units').value = '{pick}'")
    await ev("document.getElementById('o-regen').click()")

    posts = []
    for _ in range(60):
        posts = json.loads(await ev("JSON.stringify(window.__posts || [])") or "[]")
        if posts:
            break
        await asyncio.sleep(0.3)
    body = (posts[0].get("b") if posts else {}) or {}
    if pick == "keep":
        check(f"{tag} 选「保持当前」→ 请求体**不带** unit_count（沿用当前）",
              bool(posts) and "unit_count" not in body, str(posts))
    else:
        check(f"{tag} 选 {pick} 个单元 → 请求体 unit_count == {pick}",
              body.get("unit_count") == int(pick), str(posts))
    check(f"{tag} 请求体仍带 note 字段", "note" in body, str(posts))


# ── main ────────────────────────────────────────────────────────
def main() -> int:
    procs = start_services()
    try:
        if not (wait_http(f"{MOCK}/health") and wait_http(f"{BASE}/api/health")):
            print("服务未启动"); return 1
        print("服务已启动（后端 8770 / mock 8771）")
        use_model("mock-outline")
        # 新手引导默认未完成会把所有页面送去 #/welcome（2026-09-22 上线）——
        # 本套件验证的是既有页面行为，先置为完成（引导流程本身由 course_ui_check [10] 组验证）。
        httpx.put(f"{BASE}/api/settings", json={"guide": {"done": True}}, timeout=10,
                  trust_env=False)

        # ── [3] AI 材料区块：说明文字已删（区块本身仍在）──────
        print("\n[3] 「AI 材料」区块的说明文字")
        doc_id = build_material()
        check("[3] 材料已入库（有 doc_id）", bool(doc_id), doc_id)
        dom = dump(f"{BASE}/#/courses")
        check("[3] 课程页正常渲染", "data-view-error" not in dom, dom[:200])
        check("[3] AI 材料区块仍出现（说明它没被误删整块）", "AI 材料（1）" in dom,
              dom[:400])
        check("[3] 那段说明文字已删除",
              "还没用来建课的会留在这里" not in dom and "不需要了就点右侧" not in dom)

        # ── [1][2] 新建向导：状态移到按钮右侧 + 目标框 loading ──
        asyncio.run(with_page(f"{BASE}/#/courses?new=1", fn_wizard))

        # ── [4] 结构编辑页：单元数量可控 ────────────────────
        cid_a = new_course(doc_id, 2, "学会装饰器（A）")
        asyncio.run(with_page(f"{BASE}/#/courses?confirm={cid_a}",
                              lambda ev: fn_editor(ev, cid_a, 2, "5")))
        wait_course_ready(cid_a)

        cid_b = new_course(doc_id, 2, "学会装饰器（B）")
        asyncio.run(with_page(f"{BASE}/#/courses?confirm={cid_b}",
                              lambda ev: fn_editor(ev, cid_b, 2, "keep")))
        wait_course_ready(cid_b)

        # ── [4b] 真端到端：单元数真的能改出来 ────────────────
        print("\n[4b] 真端到端：重新生成后单元数是否真的变多")
        use_model("mock-outline-units")
        # 判别性用例：B 当前是 2 个单元，只在 note 里写「五章」，若能出 5 才说明 note 解析生效
        r = payload(api("POST", f"/api/courses/{cid_b}/outline:regenerate",
                        json={"note": "一共生成五章，最后一个单元要求是背诵的单元"}))
        wait_job(r["job_id"])
        c2 = wait_course_ready(cid_b)
        n_note = len((c2 or {}).get("units") or [])
        check("[4b] note 里写「一共生成五章」→ 真出 5 个单元（课程原值 2）",
              n_note == 5, f"实得 {n_note} 个单元")

        r = payload(api("POST", f"/api/courses/{cid_b}/outline:regenerate",
                        json={"note": "", "unit_count": 7}))
        wait_job(r["job_id"])
        c3 = wait_course_ready(cid_b)
        n_explicit = len((c3 or {}).get("units") or [])
        check("[4b] 显式 unit_count=7 → 真出 7 个单元",
              n_explicit == 7, f"实得 {n_explicit} 个单元")

        # 对照：自动（0）时不该被旧值钉住
        r = payload(api("POST", f"/api/courses/{cid_b}/outline:regenerate",
                        json={"note": "", "unit_count": 0}))
        wait_job(r["job_id"])
        c4 = wait_course_ready(cid_b)
        n_auto = len((c4 or {}).get("units") or [])
        check("[4b] 显式 unit_count=0（自动）→ 不被旧值 7 钉住（mock 自动模式返回 3）",
              n_auto == 3, f"实得 {n_auto} 个单元")

    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass

    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 60)
    print(f"前端四处改动端到端验证：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  失败：{f}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
