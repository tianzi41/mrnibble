"""「AI 材料 → 学习目标自动填写」链路的端到端验证（真实无头 Chrome + mock LLM）。

用户实测 bug：没有学习材料时，先让 AI 把材料写出来，再进创建向导选课型，
学习目标被填成与材料无关的内容 —— 题材是「cmd 终端和 PowerShell 的使用」，
目标却填成「读懂《出师表》全文……」。

根因（已修）：主题模式下 `autoGoal()` 与「分析材料并预选」都从 `#pick-docs`
复选框取材料 id。该面板在主题模式下整块隐藏，且 AI 材料写完后
`loadDocuments()` 只刷 `S.documents`、**不会给 `#pick-docs` 补复选框**
（向导建好时按当时的材料一次性画死）→ 取到空数组 → 后端
`_material_context` 把空数组兜底成「全部已解析材料」→ 目标按库里**无关的
旧材料**生成。提交建课那段（`T.on ? [T.docId]`）本来就是对的。

判别设计（本脚本的灵魂）：
- 库里先放一份**无关旧材料**《出师表》节选（复现用户环境里的旧资料）；
- 主题模式生成的 AI 材料，mock 的材料桩内容围绕「装饰器」（mock 的章节
  正文写死了装饰器示例），目标桩按**材料概览文本**判别题材并回显到目标句；
- 因此界面上的目标文本直接回答「这条目标基于哪份材料」：
  含「装饰器」= 基于 AI 材料（正确）；含「出师表」= 基于旧材料（bug 复现）。
- 同时用 Api.post 探针记录前端实际发出的 `document_ids`，精确断言状态传递。

覆盖链路：主题模式切页 → 材料未就绪时点课型（守卫：不发请求）→
生成目录 → 写正文 → 材料就绪后自动补目标 → 「分析材料并预选」→ 手动换课型卡。

用法::

    .venv/Scripts/python.exe dev/topic_goal_check.py
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
from course_ui_check import ui_profile  # noqa: E402
from ui_fixes_check import (  # noqa: E402
    api, payload, use_model, wait_http, with_page,
)

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".tmp" / "test-data-topicgoal"
BASE = "http://127.0.0.1:8770"
MOCK = "http://127.0.0.1:8771"
SPY = DATA / "mock-spy.json"

TOPIC = "cmd 终端和 PowerShell 的使用"
# 旧材料（与本题材无关）：复现用户库里已存在的旧资料
OLD_MD = (
    "# 出师表（节选）\n\n"
    "先帝创业未半而中道崩殂，今天下三分，益州疲弊，此诚危急存亡之秋也。"
    "然侍卫之臣不懈于内，忠志之士忘身于外者，盖追先帝之殊遇，欲报之于陛下也。"
    "诚宜开张圣听，以光先帝遗德，恢弘志士之气，不宜妄自菲薄，引喻失义，"
    "以塞忠谏之路也。\n\n"
    "宫中府中，俱为一体，陟罚臧否，不宜异同。若有作奸犯科及为忠善者，"
    "宜付有司论其刑赏，以昭陛下平明之理，不宜偏私，使内外异法也。"
).encode("utf-8")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK' if cond else 'XX'}  {name}" + (f"  | {detail}" if detail and not cond else ""))
    sys.stdout.flush()


def start_services() -> list[subprocess.Popen]:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ, "ZHIBAN_DATA_DIR": str(DATA), "ZHIBAN_PORT": "8770",
        "ZHIBAN_MOCK_SPY": str(SPY),
        "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
    }
    return [
        subprocess.Popen([str(PY), str(ROOT / "dev" / "mock_llm.py")],
                         env={**env, "ZHIBAN_MOCK_PORT": "8771"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]


def upload_old_doc() -> str:
    """上传一份与本题材无关的旧材料（《出师表》节选），返回 doc_id。"""
    with httpx.Client(trust_env=False, timeout=30) as cli:
        r = cli.post(f"{BASE}/api/documents/upload",
                     files={"files": ("出师表节选.md", OLD_MD, "text/markdown")}).json()
    doc_id = ((r.get("data") or {}).get("documents") or [{}])[0].get("id") or ""
    for _ in range(60):
        d = payload(api("GET", f"/api/documents/{doc_id}")) or {}
        if d.get("status") in ("ready", "failed"):
            break
        time.sleep(0.5)
    return doc_id


def spy_user_text() -> str:
    """mock 落盘的所有模型调用里，user 消息拼接（看材料概览实际喂了什么）。"""
    if not SPY.exists():
        return ""
    out = ""
    for ln in SPY.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            msgs = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        out += " ".join(str(m.get("content") or "") for m in msgs if m.get("role") == "user")
    return out


async def fn(ev, old_doc_id: str) -> None:
    """CDP 页面内的断言（ev = Runtime.evaluate 的封装，支持 awaitPromise）。"""
    # ── 等向导就绪，切到「没有材料，我直接说想学什么」───────
    for _ in range(60):
        if await ev("!!document.getElementById('src-topic')"):
            break
        await asyncio.sleep(0.3)
    check("S1 创建向导已打开", bool(await ev("!!document.getElementById('src-topic')")))
    await ev("document.getElementById('src-topic').click(); void 0")
    for _ in range(60):
        if await ev("document.querySelectorAll('.intent-card').length >= 2"):
            break
        await asyncio.sleep(0.3)
    check("G0 课型卡已渲染（≥2 张）",
          bool(await ev("document.querySelectorAll('.intent-card').length >= 2")))

    # ── G1 材料未就绪时点课型卡：守卫必须拦住（不发请求、只提示）────
    await ev("document.querySelector('.intent-card').click(); void 0")
    hint = ""
    for _ in range(30):
        hint = await ev("(document.getElementById('f-goal-hint')||{}).textContent || ''")
        if "材料写好后" in (hint or ""):
            break
        await asyncio.sleep(0.3)
    check("G1 材料未就绪时点课型 → 提示等待而不发请求（守卫生效）",
          "材料写好后" in (hint or ""), str(hint))
    check("G1b 守卫期间没有向模型发过请求（spy 无落盘）",
          not SPY.exists() or not SPY.read_text(encoding="utf-8").strip(),
          "spy 已有落盘")

    # ── 生成目录 → 写正文（AI 材料完全由界面流程生成）──────────
    await ev("""(function(){
        var i = document.getElementById('f-topic');
        i.value = %r; i.dispatchEvent(new Event('input', {bubbles:true}));
        return true;})()""" % TOPIC)
    await ev("document.getElementById('t-outline').click(); void 0")
    for _ in range(80):
        if await ev("document.querySelectorAll('#t-chapters input').length >= 3"):
            break
        await asyncio.sleep(0.5)
    check("S2 材料目录已生成（≥3 章）",
          bool(await ev("document.querySelectorAll('#t-chapters input').length >= 3")))
    await ev("document.getElementById('t-write').click(); void 0")
    status = ""
    for _ in range(240):
        status = await ev("(document.getElementById('t-status')||{}).textContent || ''")
        if "材料已就绪" in (status or ""):
            break
        await asyncio.sleep(0.5)
    check("S3 AI 材料已写完并就绪", "材料已就绪" in (status or ""), str(status)[:120])

    # ── 页内定位 AI 材料 id（排除旧材料；库里此时应恰为两份）──────
    docs_info = await ev("""(async function(){
        var d = await Api.get("/api/documents?page=1&page_size=200");
        var items = (d && d.items) || [];
        var ai = items.filter(function(x){ return x.id !== %r; });
        return {n: items.length, aiId: ai.length ? ai[0].id : "",
                titles: items.map(function(x){ return x.title; })};})()""" % old_doc_id)
    ai_doc_id = (docs_info or {}).get("aiId") or ""
    check("S3b AI 材料已入库（库里恰为旧材料 + AI 材料两份）",
          (docs_info or {}).get("n") == 2 and bool(ai_doc_id),
          f"n={(docs_info or {}).get('n')} ai={ai_doc_id} titles={(docs_info or {}).get('titles')}")
    if not ai_doc_id:
        return

    # ── G2 材料就绪后自动补目标（先选了课型的顺序也覆盖到）──────
    goal = ""
    for _ in range(60):
        goal = await ev("(document.getElementById('f-goal')||{}).value || ''")
        if goal and goal.strip():
            break
        await asyncio.sleep(0.5)
    check("G2a 材料就绪后目标被自动填写（无需再手点）", bool((goal or "").strip()),
          str(goal)[:120])
    check("G2b 目标基于 **AI 生成的材料**（含「装饰器」，mock 材料桩题材）",
          "装饰器" in (goal or "") and "出师表" not in (goal or ""), str(goal)[:120])

    # ── 探针：记录前端实际发出的 document_ids ──────────────────
    await ev("""(function(){
        window.__reqs = [];
        window.__post = Api.post;
        Api.post = function(url, body){
            var u = String(url);
            if (u.indexOf('suggest-goal') >= 0 || u.indexOf('suggest-intents') >= 0) {
                window.__reqs.push({url: u, ids: (body || {}).document_ids});
            }
            return window.__post.apply(this, arguments);
        };
        window.__aiId = %r;
        return true;})()""" % ai_doc_id)

    # ── G3「分析材料并预选」（suggest-intents + autoGoal 两次调用）──
    before = await ev("window.__reqs.length")
    await ev("""(function(){var b=document.getElementById('f-analyze');
        if(!b) return false; b.click(); return true;})()""")
    reqs = []
    for _ in range(60):
        reqs = await ev("window.__reqs") or []
        if len(reqs) >= (before or 0) + 2:
            break
        await asyncio.sleep(0.5)
    check("G3a 「分析材料并预选」发出了请求（intents + goal）", len(reqs) >= 2,
          f"{len(reqs)} 条")
    bad = [r for r in reqs if list(r.get("ids") or []) != [ai_doc_id]]
    check("G3b 两次请求的 document_ids 都精确等于 AI 材料的 id（不是空数组、不含旧材料）",
          not bad, str(bad)[:160])
    goal2 = ""
    for _ in range(30):
        goal2 = await ev("(document.getElementById('f-goal')||{}).value || ''")
        if "装饰器" in (goal2 or ""):
            break
        await asyncio.sleep(0.3)
    check("G3c 分析后重填的目标仍基于 AI 材料",
          "装饰器" in (goal2 or "") and "出师表" not in (goal2 or ""), str(goal2)[:120])

    # ── G4 手动换一张课型卡 ────────────────────────────────────
    before2 = await ev("window.__reqs.length")
    await ev("""(function(){
        var cs = document.querySelectorAll('.intent-card');
        if (cs.length < 2) return false; cs[1].click(); return true;})()""")
    reqs2 = []
    for _ in range(60):
        reqs2 = await ev("window.__reqs") or []
        if len(reqs2) >= (before2 or 0) + 1:
            break
        await asyncio.sleep(0.5)
    check("G4a 手动换课型卡也发出了目标请求", len(reqs2) >= 1, f"{len(reqs2)} 条")
    bad2 = [r for r in reqs2 if list(r.get("ids") or []) != [ai_doc_id]]
    check("G4b 手动换卡请求的 document_ids 同样精确等于 AI 材料的 id", not bad2,
          str(bad2)[:160])
    goal3 = ""
    for _ in range(30):
        goal3 = await ev("(document.getElementById('f-goal')||{}).value || ''")
        if "装饰器" in (goal3 or ""):
            break
        await asyncio.sleep(0.3)
    check("G4c 换卡后的目标仍基于 AI 材料",
          "装饰器" in (goal3 or "") and "出师表" not in (goal3 or ""), str(goal3)[:120])

    # ── G5 后端侧证据：模型收到的材料概览只有 AI 材料 ───────────
    text = spy_user_text()
    check("G5 模型收到的材料概览含 AI 材料题材（装饰器）且完全不含旧材料（出师表）",
          "装饰器" in text and "出师表" not in text, text[:160])


async def _run() -> int:
    procs = start_services()
    try:
        assert wait_http(f"{BASE}/api/health") and wait_http(f"{MOCK}/health"), "服务未启动"
        use_model("mock-outline")
        old_doc_id = upload_old_doc()
        check("S0 旧材料（出师表）已入库且解析完成", bool(old_doc_id), old_doc_id)

        # 新库默认 guide.done=false，首次启动拦截（main.js route() 的设计行为）
        # 会把 #/courses 也送去新手引导 —— 本套件驱动的是课程页，先标记引导完成
        # （与 course_ui_check 同一模式；引导流程本身由 course_ui_check 10.13–10.16 覆盖）。
        with httpx.Client(trust_env=False, timeout=10) as cli:
            cli.put(f"{BASE}/api/settings", json={"guide": {"done": True}})

        async def fn3(ev) -> None:
            await fn(ev, old_doc_id)

        await with_page(BASE + "/#/courses?new=1", fn3)
    finally:
        for p in procs:
            p.terminate()

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  失败：" + f)
    sys.stdout.flush()
    os._exit(0 if not FAIL else 1)   # 硬退出：同 launcher_liveness_check 的坑


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
