"""「学完最后一课」收尾体验的端到端验证（真实无头 Chrome + mock LLM）。

用户反馈：上完最后一课后界面还停在那一课，不仔细看以为没学完 —— 应给出
「你已经学完本课程，恭喜」之类的收尾。本脚本验证三层收尾：

  [C] 判别性对照：课程还有未完成讲次时，**不**出现结课横幅（证明横幅由完成度驱动，
      不是写死的文案）。
  [A] 在最后一讲点「完成本讲」→ **留在原地**给出结课庆祝（🏆 恭喜 + 全部完成），
      不再像旧行为那样把用户甩回课程列表 + 一句就消失的 toast。
  [B] 重新进入最后一讲（模拟改天再来）→ 顶部出现结课横幅，并带「返回课程大纲」出口。

为什么点「完成本讲」就够了：结课路径由 completeLesson 驱动（标记 done → 判断有无
下一讲），不需要真的把朗读跑完 —— 无头环境里等音频播完既慢又不稳。

用法::
    .venv/Scripts/python.exe dev/course_done_check.py
"""

from __future__ import annotations

import asyncio
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
    api, build_material, new_course, payload, use_model, wait_course_ready,
    wait_gen, wait_http, wait_job, with_page,
)

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".tmp" / "test-data-coursedone"
BASE = "http://127.0.0.1:8770"
MOCK = "http://127.0.0.1:8771"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = 9228

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK' if cond else 'XX'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


def start_services() -> list[subprocess.Popen]:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ, "ZHIBAN_DATA_DIR": str(DATA), "ZHIBAN_PORT": "8770",
        "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
    }
    return [
        subprocess.Popen([str(PY), str(ROOT / "dev" / "mock_llm.py")],
                         env={**env, "ZHIBAN_MOCK_PORT": "8771"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]


def flatten_lessons(course: dict) -> list[dict]:
    out: list[dict] = []
    for u in course.get("units") or []:
        out.extend(u.get("lessons") or [])
    return out


BANNER_PROBE = ("(function(){var b=document.querySelector('.course-done-banner');"
                "return b?b.textContent:'';})()")
CARD_PROBE = ("(function(){var c=[].map.call(document.querySelectorAll('.teach-card'),"
              "function(x){return x.textContent;}).join(' ');"
              "return {card:c, hash:location.hash};})()")


async def fn(ev, last_id: str) -> None:
    """CDP 页面内的断言（ev = Runtime.evaluate 的封装）。"""
    for _ in range(60):
        if await ev("!!document.getElementById('lesson-head')"):
            break
        await asyncio.sleep(0.3)
    for _ in range(40):
        if await ev("!!document.getElementById('b-done')"):
            break
        await asyncio.sleep(0.3)
    check("S1 最后一讲页有「完成本讲」按钮（讲义已生成）",
          bool(await ev("!!document.getElementById('b-done')")))

    # [C] 判别性对照：还有未完成讲次 → 不出现结课横幅
    dom = await ev("document.body.innerText")
    check("C1 未全部完成时不显示结课横幅（判别性对照）",
          "这门课你已经学完了" not in (dom or ""))

    # [A] 点「完成本讲」→ 留在原地 + 结课庆祝
    #     ⚠️ 页面加载后有多次异步重渲染（会话绑定/材料元数据等），
    #     「查询到按钮」和「点击」之间按钮可能被重建 —— 所以点击时**重查重试**。
    hash_before = await ev("location.hash")
    clicked = False
    for _ in range(20):
        clicked = bool(await ev(
            "(function(){var b=document.getElementById('b-done');"
            "if(!b) return false; b.click(); return true;})()"))
        if clicked:
            break
        await asyncio.sleep(0.3)
    check("A0 「完成本讲」按钮点到了（重查重试，避开重渲染竞态）", clicked)
    card, hash_after = "", ""
    for _ in range(20):
        r = await ev(CARD_PROBE)
        if r and "恭喜" in (r.get("card") or ""):
            card, hash_after = r.get("card") or "", r.get("hash") or ""
            break
        await asyncio.sleep(0.3)
    check("A1 完成最后一讲 → 留在原地给出结课庆祝（不再甩回列表）",
          bool(card) and "恭喜" in card and "全部完成" in card, str(card)[:160])
    check("A2 页面没有跳走（hash 不变）", hash_after == hash_before,
          f"{hash_before} → {hash_after}")

    # [B] 重新进入最后一讲（模拟改天再来）→ 顶部结课横幅
    await ev("location.hash = '#/courses'; void 0")
    await asyncio.sleep(0.8)
    await ev("location.hash = '#/lessons/" + last_id + "'; void 0")
    banner = ""
    for _ in range(40):
        banner = await ev(BANNER_PROBE)
        if banner and "学完" in (banner or ""):
            break
        await asyncio.sleep(0.3)
    check("B1 重新进入最后一讲 → 顶部出现结课横幅",
          bool(banner) and "学完" in (banner or ""), str(banner))
    check("B2 横幅带「返回课程大纲」出口",
          bool(await ev("!!document.getElementById('b-done-list')")))


async def _run() -> int:
    procs = start_services()
    try:
        assert wait_http(f"{BASE}/api/health") and wait_http(f"{MOCK}/health"), "服务未启动"
        # 新手引导默认未完成会把所有页面送去 #/welcome（2026-09-22 上线）——
        # 本套件走的是课程/课堂页，先置为完成（引导流程由 course_ui_check [10] 组验证）。
        api("PUT", "/api/settings", json={"guide": {"done": True}})
        use_model("mock-outline")
        doc = build_material()
        cid = new_course(doc, unit_count=1, goal="快速过一遍装饰器")
        course = wait_course_ready(cid)

        # mock 大纲的最后一讲固定是「练习」—— 结课体验要落在**讲解**上才有意义。
        # 用结构确认接口把每单元末尾的练习讲删掉（等同用户在结构编辑页删讲），
        # 这样课程的最后一讲就是一节讲解，走「完成本讲」的结课路径。
        units = []
        for u in course.get("units") or []:
            ls = u.get("lessons") or []
            units.append({"title": u["title"], "summary": u.get("summary"),
                          "lessons": [{"title": l["title"], "objective": l.get("objective"),
                                       "kind": l["kind"], "depth": l.get("depth")}
                                      for l in ls[:-1]]})
        api("POST", f"/api/courses/{cid}/outline:confirm",
            json={"title": course["title"], "units": units})
        course = wait_course_ready(cid)
        lessons = flatten_lessons(course)
        check("S0 课程就绪且最后一讲是讲解（≥2 讲）",
              len(lessons) >= 2 and lessons[-1].get("kind") == "lecture",
              str([(l.get("kind")) for l in lessons]))
        last = lessons[-1]

        # 最后一讲生成讲义（没有 board 就没有「完成本讲」按钮）
        use_model("mock-lecture")
        job = payload(api("POST", f"/api/courses/lessons/{last['id']}/lecture"))
        wait_job(job["job_id"])
        lesson_now = payload(api("GET", f"/api/courses/lessons/{last['id']}"))
        check("S2 最后一讲讲义已生成", bool(lesson_now.get("board")), str(lesson_now)[:120])

        # 除最后一讲外，其余讲次标记完成
        for l in lessons[:-1]:
            api("PUT", f"/api/courses/lessons/{l['id']}", json={"complete": True})

        last_id = last["id"]

        async def fn3(ev) -> None:
            await fn(ev, last_id)

        import ui_fixes_check as uf

        await uf.with_page(BASE + "/#/lessons/" + last_id, fn3)
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
