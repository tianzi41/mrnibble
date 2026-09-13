"""前端页面冒烟：无头 Chrome 加载课程相关页面，检查是否有 JS 运行时错误。

用法::

    .venv/Scripts/python.exe dev/course_ui_check.py
"""

from __future__ import annotations

import io
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
        check("1.8b 有目标建议按钮", "帮我推荐" in dom)
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

            # 完成本单元全部讲次 → 单元总结可生成（供单元总结页签验证）
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

        # 授课舞台：屏幕中下方字幕 + 互动选择（原来在右栏角落，用户看不见）
        dom_lesson = dump(f"{BASE}/#/lessons/{lesson_id}")
        check("2.28 授课舞台容器存在", 'id="teach-stage"' in dom_lesson)
        check("2.29 字幕行存在", 'id="teach-sub"' in dom_lesson)
        check("2.30 互动选择容器存在", 'id="teach-ask"' in dom_lesson)
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
