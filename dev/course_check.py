"""课程链路自测（无网络、无真实 Key、可重复）。

覆盖：「创建课程 → 大纲生成 → 结构确认 → 白板讲义 → 随堂练习 → 判分 → 进度」
以及两条红线（引用页码不伪造、模型失败时降级不空课）。

用法::

    .venv/Scripts/python.exe dev/course_check.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from make_pdf import make_text_pdf

ROOT = Path(__file__).resolve().parents[1]
# P0 回归组直接 import backend（子进程的 PYTHONPATH 只在子树内有效，主进程要自己加）。
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".tmp" / "test-data-course"
BACKEND = "http://127.0.0.1:8762"
MOCK = "http://127.0.0.1:8763"
FAKE_KEY = "sk-test-course-abcdef123456"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{('  ← ' + detail) if (detail and not cond) else ''}")
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


def get(path: str, **kw):
    # trust_env=False：本机有系统代理（HTTP_PROXY），会把 127.0.0.1 的请求也丢给代理。
    with httpx.Client(trust_env=False) as cli:
        return cli.get(f"{BACKEND}{path}", timeout=20, **kw).json()


def post(path: str, body=None, **kw):
    with httpx.Client(trust_env=False) as cli:
        return cli.post(f"{BACKEND}{path}", json=body or {}, timeout=30, **kw).json()


def put(path: str, body=None, **kw):
    with httpx.Client(trust_env=False) as cli:
        return cli.put(f"{BACKEND}{path}", json=body or {}, timeout=20, **kw).json()


def set_model(model: str) -> None:
    r = put("/api/settings", {"llm": {"model": model}})
    assert r["code"] == 0, r


def wait_job(job_id: str, tries: int = 120) -> dict:
    """轮询生成任务，返回最终任务对象。"""
    for _ in range(tries):
        job = get(f"/api/courses/jobs/{job_id}")["data"]
        if job["status"] in ("ready", "failed"):
            return job
        time.sleep(0.5)
    return {"status": "timeout"}


def main() -> int:
    if DATA.exists():
        # 沙箱批量删除护栏会拦 rmtree（turn 级删除预算有限，删过大目录后连
        # 测试目录的清理也会被拦，整个脚本被拖死）：先改名腾位（瞬时、不走删除），
        # 旧目录尽力清理，失败就留待手动/下次清理。
        stale = DATA.with_name(DATA.name + ".old")
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)
        DATA.rename(stale)
        shutil.rmtree(stale, ignore_errors=True)
    DATA.mkdir(parents=True)

    env = {**os.environ, "ZHIBAN_DATA_DIR": str(DATA), "ZHIBAN_PORT": "8762",
           "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    procs = [
        # mock 端口用 ZHIBAN_MOCK_PORT 固定为 8763，与主自测（8761）互不干扰。
        # ZHIBAN_MOCK_SPY：mock-spy-* 模型把收到的 messages 落到此文件（验证提示词注入）。
        subprocess.Popen([str(PY), str(ROOT / "dev" / "mock_llm.py")],
                         env={**env, "ZHIBAN_MOCK_PORT": "8763",
                              "ZHIBAN_MOCK_SPY": str(DATA / "mock-spy.json")},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        assert wait_http(f"{MOCK}/health"), "mock LLM 未启动"
        assert wait_http(f"{BACKEND}/api/health"), "后端未启动"
        print("服务已启动（mock 8763 / 后端 8762）\n")

        # ── A. 准备材料 ────────────────────────────────
        print("[A] 准备学习材料")
        r = put("/api/settings", {
            "llm": {"base_url": f"{MOCK}/v1", "model": "mock-outline", "api_key": FAKE_KEY}
        })
        check("A1 保存模型配置", r["code"] == 0, r.get("message", ""))
        md = io.BytesIO((
            "# 第3章 极限与洛必达法则\n\n"
            "## 3.1 极限的定义\n\n"
            "极限描述函数在某点附近的变化趋势，当自变量趋于某点时，函数值无限接近某个确定的数。\n\n"
            "## 3.2 洛必达法则\n\n"
            "洛必达法则是求未定式极限的重要方法，它适用于 0/0 型或 ∞/∞ 型未定式。"
            "使用前提是分子分母同时趋于零或同时趋于无穷大，使用前必须先验证类型。\n\n"
            "## 3.3 连续与间断\n\n"
            "若函数在某点的极限值等于函数值，则称函数在该点连续。"
        ).encode("utf-8"))
        with httpx.Client(trust_env=False) as cli:
            r = cli.post(f"{BACKEND}/api/documents/upload",
                         files={"files": ("高数-第3章.md", md, "text/markdown")},
                         timeout=30).json()
        check("A2 上传成功", r["code"] == 0, r.get("message", ""))
        doc_id = r["data"]["documents"][0]["id"]
        for _ in range(60):
            d = get(f"/api/documents/{doc_id}")["data"]["document"]
            if d["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        check("A3 解析完成", d["status"] == "ready", str(d.get("error")))

        # ── A4. 心跳 ───────────────────────────────────
        print("\n[A4] 心跳")
        r = post("/api/heartbeat")
        check("A4 心跳返回 ok", r["code"] == 0 and r["data"].get("ok") is True, str(r))

        # ── A5. 深度思考设置 ────────────────────────────
        print("\n[A5] 深度思考设置")
        r = put("/api/settings", {"llm": {"enable_thinking": True}})
        check("A5 保存深度思考开关", r["code"] == 0, r.get("message", ""))
        s = get("/api/settings")["data"]["llm"]
        check("A6 读取到 enable_thinking", s.get("enable_thinking") is True, str(s))
        # 恢复默认，避免影响后续测试（合并写入，不影响其他字段）
        put("/api/settings", {"llm": {"enable_thinking": False}})

        # ── B. 创建课程 + 大纲生成 ─────────────────────
        print("\n[B] 创建课程 / 生成大纲")
        r = post("/api/courses", {
            "goal": "学完能判断什么时候可以用洛必达法则，并独立完成典型题",
            "document_ids": [doc_id], "level": "beginner", "depth": "standard",
            "unit_count": 2,
        })
        check("B1 创建课程成功", r["code"] == 0, r.get("message", ""))
        course_id = r["data"]["course_id"]
        job = wait_job(r["data"]["job_id"])
        check("B2 大纲任务完成", job.get("status") == "ready", str(job))
        course = get(f"/api/courses/{course_id}")["data"]
        check("B3 课程状态为 ready", course["status"] == "ready", course.get("error", ""))
        check("B4 生成 2 个单元", len(course["units"]) == 2, str(len(course["units"])))
        lessons = [l for u in course["units"] for l in u["lessons"]]
        check("B5 每个单元 3 个讲次", len(lessons) == 6, str(len(lessons)))
        check("B6 末节为练习", all(u["lessons"][-1]["kind"] == "practice" for u in course["units"]))
        check("B7 进度初始为 0", course["progress"]["percent"] == 0.0,
              str(course["progress"]))

        # ── B2. 自动单元数 ─────────────────────────────
        print("\n[B2] 自动单元数")
        set_model("mock-outline-5")
        r = post("/api/courses", {
            "goal": "自动单元数测试", "document_ids": [doc_id],
        })
        check("B8 自动单元数创建成功", r["code"] == 0, r.get("message", ""))
        auto_cid = r["data"]["course_id"]
        job = wait_job(r["data"]["job_id"])
        check("B9 大纲任务完成", job.get("status") == "ready", str(job))
        auto_course = get(f"/api/courses/{auto_cid}")["data"]
        check("B10 自动单元数 = 5", len(auto_course["units"]) == 5, str(len(auto_course["units"])))

        # ── B3. 目标建议 ───────────────────────────────
        print("\n[B3] 目标建议")
        set_model("mock-goals")
        r = post("/api/courses/suggest-goals", {"document_ids": [doc_id]})
        check("B11 建议接口成功", r["code"] == 0, r.get("message", ""))
        goals = r["data"].get("goals") or []
        check("B12 返回 4 个目标", len(goals) == 4, str(len(goals)))
        check("B13 每个目标非空", all(g for g in goals), str(goals))

        # 清理自动单元数测试课程
        with httpx.Client(trust_env=False) as cli:
            cli.delete(f"{BACKEND}/api/courses/{auto_cid}", timeout=20)

        # 恢复后续测试所需模型
        set_model("mock-outline")

        # ── C. 结构确认（含手工改名）────────────────────
        print("\n[C] 确认课程结构")
        units = []
        for u in course["units"]:
            units.append({
                "title": u["title"], "summary": u["summary"],
                "lessons": [{"title": l["title"], "objective": l["objective"],
                             "kind": l["kind"], "depth": l["depth"]}
                            for l in u["lessons"][:-1]],  # 故意删掉每单元最后一节
            })
        r = post(f"/api/courses/{course_id}/outline:confirm", {"title": "极限入门", "units": units})
        check("C1 确认成功", r["code"] == 0, r.get("message", ""))
        course = get(f"/api/courses/{course_id}")["data"]
        check("C2 标题已更新", course["title"] == "极限入门", course["title"])
        lessons = [l for u in course["units"] for l in u["lessons"]]
        check("C3 删除讲次生效", len(lessons) == 4, str(len(lessons)))
        check("C4 全局序号连续", [l["global_ordinal"] for l in lessons] == [1, 2, 3, 4],
              str([l["global_ordinal"] for l in lessons]))

        # ── D. 白板讲义 ────────────────────────────────
        print("\n[D] 生成白板讲义")
        set_model("mock-lecture")
        lesson_id = lessons[0]["id"]
        r = post(f"/api/courses/lessons/{lesson_id}/lecture")
        check("D1 讲义任务已创建", r["code"] == 0, r.get("message", ""))
        job = wait_job(r["data"]["job_id"])
        check("D2 讲义任务完成", job.get("status") == "ready", str(job))
        lesson = get(f"/api/courses/lessons/{lesson_id}")["data"]
        check("D3 讲次状态推进", lesson["status"] == "lecture_ready", lesson["status"])
        board = lesson["board"] or {}
        check("D4 白板含卡片", len(board.get("cards") or []) >= 3,
              str(len(board.get("cards") or [])))
        check("D5 关键术语 ≥2", len(board.get("keypoints") or []) >= 2)
        check("D6 讲义有 Markdown", bool(lesson["board_md"]))
        cites = lesson["citations"] or []
        check("D7 引用已回填", len(cites) >= 1, str(len(cites)))
        check("D8 引用来自 DB（有文档标题）",
              all(c.get("document_title") for c in cites), json.dumps(cites[:1], ensure_ascii=False))
        check("D9 正文无残留 [[c:N]]", "[[c:" not in json.dumps(board, ensure_ascii=False))

        # ── D3. 课件 / 讲稿分离（P2）─────────────────────
        print("\n[D3] 课件与讲师讲稿分离")
        slides = lesson.get("slides") or []
        scripts = lesson.get("scripts") or []
        check("D12 讲次带出独立课件", len(slides) >= 3, str(len(slides)))
        check("D13 讲次带出独立讲稿", len(scripts) >= 3, str(len(scripts)))
        check("D14 课件讲稿逐页对应",
              len(slides) == len(scripts)
              and {s["id"] for s in slides} == {sc["slide_id"] for sc in scripts},
              f"{len(slides)} slides vs {len(scripts)} scripts")
        check("D15 课件是短要点", all(
            sum(len(b) for b in (s.get("bullets") or [])) + len(s.get("body") or "") < 400
            for s in slides), json.dumps(slides[0], ensure_ascii=False)[:160] if slides else "")
        # 讲稿应是对课件的展开（更长），而不是简单复制课件内容
        def _slide_text(s):
            return "".join([s.get("title") or ""] + (s.get("bullets") or []) + [s.get("body") or ""])
        same = sum(1 for s, sc in zip(slides, scripts)
                   if (sc.get("text") or "").strip() == _slide_text(s).strip())
        longer = sum(1 for s, sc in zip(slides, scripts)
                     if len((sc.get("text") or "")) > len(_slide_text(s)))
        check("D16 讲稿不是课件的复制", same == 0, f"{same}/{len(slides)} 页相同")
        check("D17 讲稿比课件更展开", longer >= 2, f"{longer}/{len(slides)} 页更长")
        check("D18 引用含脚本回填",
              any("极限" in (c.get("snippet") or "") or "洛必达" in (c.get("snippet") or "")
                  for c in cites), json.dumps(cites, ensure_ascii=False)[:160])
        # 独立更新接口：课件/讲稿可单独保存且 slide_id 关联保持
        r = put(f"/api/courses/lessons/{lesson_id}/courseware", {
            "slides": [{"id": "slide-1", "kind": "concept", "title": "编辑后的标题",
                        "bullets": ["要点甲", "要点乙"], "body": ""}],
            "scripts": [{"slide_id": "slide-1", "text": "这是编辑后的讲稿。"}],
        })
        check("D19 课件讲稿独立更新", r["code"] == 0, r.get("message", ""))
        l2d = get(f"/api/courses/lessons/{lesson_id}")["data"]
        check("D20 更新后讲稿保持关联",
              l2d["scripts"][0]["slide_id"] == l2d["slides"][0]["id"],
              json.dumps(l2d["scripts"][:1], ensure_ascii=False))

        # ── D2. 材料标注（P1）：md 无页码 → 不产生标注 ────
        print("\n[D2] 材料标注（非分页材料）")
        check("D10 无页码材料不产生标注", (board.get("marks") or []) == [],
              json.dumps(board.get("marks"), ensure_ascii=False))
        r = get(f"/api/courses/lessons/{lesson_id}/marks")
        check("D11 标注读取接口可用（空）",
              r["code"] == 0 and r["data"]["marks"] == [], str(r.get("code")))

        # ── E. 随堂练习 + 判分 ─────────────────────────
        print("\n[E] 随堂练习与判分")
        set_model("mock-practice")
        r = post(f"/api/courses/lessons/{lesson_id}/practice", {"count": 5})
        check("E1 出题任务已创建", r["code"] == 0, r.get("message", ""))
        job = wait_job(r["data"]["job_id"])
        check("E2 出题任务完成", job.get("status") == "ready", str(job))
        qs = get(f"/api/courses/lessons/{lesson_id}/practice")["data"]["items"]
        check("E3 生成 5 道题", len(qs) == 5, str(len(qs)))
        check("E4 题型覆盖 4 类", {q["type"] for q in qs} == {"single", "boolean", "fill_in", "open"},
              str({q["type"] for q in qs}))
        check("E5 列表不泄露答案", all("answer" not in q for q in qs))
        check("E6 非分页材料不产生图片题", not any(q.get("image") for q in qs))

        by_type = {q["type"]: q for q in qs}
        set_model("mock-grade")
        r = post(f"/api/courses/lessons/{lesson_id}/grade", {"answers": [
            {"question_id": by_type["single"]["id"], "answer": 0},      # 正确
            {"question_id": by_type["boolean"]["id"], "answer": 1},     # 错误
            {"question_id": by_type["fill_in"]["id"], "answer": "0/0 型 与 ∞/∞ 型"},  # 正确
            {"question_id": by_type["open"]["id"], "answer": "先验证是否为未定式"},
        ]})
        check("E7 判分成功", r["code"] == 0, r.get("message", ""))
        res = r["data"]
        check("E8 客观题判分正确", res["correct_count"] >= 2, json.dumps(res["correct_count"]))
        check("E9 总分在合理区间", 0 < res["score"] <= res["total"], str(res["score"]))
        check("E10 每题都有反馈", all(x["feedback"] for x in res["results"]))
        single = next(x for x in res["results"] if x["type"] == "single")
        check("E11 单选判为正确", single["correct"] is True, str(single))
        boolean = next(x for x in res["results"] if x["type"] == "boolean")
        check("E12 判断判为错误", boolean["correct"] is False, str(boolean))
        check("E13 首次作答记 attempt_no=1", res.get("attempt_no") == 1, str(res.get("attempt_no")))

        # 重做一次：attempt_no 递增，统计按最近一次汇总
        r = post(f"/api/courses/lessons/{lesson_id}/grade", {"answers": [
            {"question_id": by_type["single"]["id"], "answer": 0},
        ]})
        check("E14 重做记为第 2 次作答", r["data"].get("attempt_no") == 2,
              str(r["data"].get("attempt_no")))

        # ── F. 进度 ────────────────────────────────────
        print("\n[F] 学习进度")
        course = get(f"/api/courses/{course_id}")["data"]
        p = course["progress"]
        check("F1 完成 1 节", p["done_lessons"] == 1, json.dumps(p))
        check("F2 百分比正确", p["percent"] == 25.0, str(p["percent"]))
        check("F3 当前讲次指向下一节", p["current_lesson_id"] != lesson_id)
        unit = course["units"][0]
        check("F4 单元未完成（还有其他讲次）", unit["status"] != "done", unit["status"])

        # ── F2. 标记完成（无练习的讲次）─────────────────
        nxt = [l for l in lessons if l["id"] != lesson_id][0]
        r = put(f"/api/courses/lessons/{nxt['id']}", {"complete": True})
        check("F5 标记完成成功", r["code"] == 0, r.get("message", ""))
        course = get(f"/api/courses/{course_id}")["data"]
        check("F6 完成数 +1", course["progress"]["done_lessons"] == 2,
              str(course["progress"]))
        # 把第一单元剩余讲次也完成，验证单元完成与下一单元激活。
        for l in lessons:
            if l["id"] != nxt["id"] and l["unit_id"] == lessons[0]["unit_id"]:
                put(f"/api/courses/lessons/{l['id']}", {"complete": True})
        course = get(f"/api/courses/{course_id}")["data"]
        check("F7 单元完成", course["units"][0]["status"] == "done",
              course["units"][0]["status"])
        check("F8 下一单元被激活", course["units"][1]["status"] == "active",
              course["units"][1]["status"])

        # ── F3. 单元统计与单元总结（P1）─────────────────
        print("\n[F3] 单元统计与单元总结")
        u0 = course["units"][0]
        st = u0.get("stats") or {}
        check("F9 单元含练习统计", st.get("questions", 0) >= 1, json.dumps(st, ensure_ascii=False))
        check("F10 统计含错题数", "errors" in st, json.dumps(st))
        unit_id = u0["id"]

        set_model("mock-summary")
        r = post(f"/api/courses/units/{unit_id}/summary")
        check("F11 总结任务已创建", r["code"] == 0, r.get("message", ""))
        job = wait_job(r["data"]["job_id"])
        check("F12 总结任务完成", job.get("status") == "ready", str(job))
        s = get(f"/api/courses/units/{unit_id}/summary")["data"]
        check("F13 总结状态 ready", s["status"] == "ready", str(s.get("error")))
        check("F14 总结含薄弱点", bool((s.get("summary") or {}).get("weak_points")),
              json.dumps(s.get("summary"), ensure_ascii=False)[:200])
        check("F15 总结含统计", bool((s.get("summary") or {}).get("stats")))
        check("F16 总结有 Markdown", bool(s.get("markdown")))
        course = get(f"/api/courses/{course_id}")["data"]
        check("F17 课程详情带出总结状态",
              course["units"][0]["summary_status"] == "ready",
              course["units"][0]["summary_status"])
        check("F18 课程详情带出总结内容",
              bool(course["units"][0]["summary_data"]))

        # 未学完的单元：允许生成（给模型提示未学完），但完全没学过则拒绝
        u1 = course["units"][1]
        r = post(f"/api/courses/units/{u1['id']}/summary")
        check("F19 未学过的单元拒绝生成", r["code"] == 1002, str(r.get("code")))

        # ── F4. 导出（P1）───────────────────────────────
        print("\n[F4] 导出")
        with httpx.Client(trust_env=False) as cli:
            r = cli.get(f"{BACKEND}/api/courses/lessons/{lesson_id}/export?kind=lesson")
            text = r.text
            check("F20 导出讲义成功", r.status_code == 200 and "# " in text, str(r.status_code))
            check("F21 导出含练习题", "随堂练习" in text or "作答" in text, text[:120])
            r2 = cli.get(f"{BACKEND}/api/courses/lessons/{lesson_id}/export?kind=conversation")
            check("F22 无对话时导出被拒", r2.status_code == 409, str(r2.status_code))
            r3 = cli.get(f"{BACKEND}/api/courses/documents/{doc_id}/raw")
            check("F23 非 PDF 材料取原文被拒", r3.status_code == 415, str(r3.status_code))
            r4 = cli.get(f"{BACKEND}/api/courses/documents/{doc_id}/page?page_no=1")
            check("F24 非 PDF 页渲染被拒", r4.status_code == 415, str(r4.status_code))

        # ── P. PDF 材料：材料标注 + 图片题 + 原页（P1）──
        print("\n[P] PDF 材料：标注 / 图片题 / 原页")
        # 内容要足够长（切片器 max_chars=800），否则只切出 1 个切片，
        # 引用表就只有 1 项，mock 里引用编号 2 的标注会被当作越界丢弃。
        pdf_bytes = io.BytesIO(make_text_pdf([
            "Chapter 3 Limits and the L Hopital rule",
            "A limit describes the trend of a function near a given point.",
            "When x approaches a value, if the function value approaches a fixed",
            "number, that number is the limit of the function at that point.",
            "The L Hopital rule is an important method for indeterminate limits.",
            "It applies to the 0/0 form and to the infinity over infinity form.",
            "The precondition is that numerator and denominator both approach zero",
            "or both approach infinity at the same time.",
            "You must verify the indeterminate form before applying the rule,",
            "otherwise you will reach a wrong conclusion.",
            "A function is continuous at a point when the limit equals the value.",
            "Continuity lets us swap the limit and the function evaluation.",
            "Typical exercises combine limits, continuity and the rule together.",
            "Always check the conditions first, then compute step by step.",
        ]))
        with httpx.Client(trust_env=False) as cli:
            r = cli.post(f"{BACKEND}/api/documents/upload",
                         files={"files": ("calculus.pdf", pdf_bytes, "application/pdf")},
                         timeout=30).json()
        check("P1 PDF 上传成功", r["code"] == 0, r.get("message", ""))
        pdf_id = r["data"]["documents"][0]["id"]
        for _ in range(60):
            d = get(f"/api/documents/{pdf_id}")["data"]["document"]
            if d["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        check("P2 PDF 解析完成", d["status"] == "ready", str(d.get("error")))

        with httpx.Client(trust_env=False) as cli:
            r = cli.get(f"{BACKEND}/api/courses/documents/{pdf_id}/raw")
            check("P3 可取 PDF 原文", r.status_code == 200
                  and r.headers.get("content-type", "").startswith("application/pdf"),
                  f"{r.status_code} {r.headers.get('content-type')}")
            check("P4 原文是合法 PDF", r.content[:5] == b"%PDF-", str(r.content[:8]))

        set_model("mock-outline")
        r = post("/api/courses", {"goal": "PDF 标注测试", "document_ids": [pdf_id],
                                  "unit_count": 2})
        pdf_course = r["data"]["course_id"]
        job = wait_job(r["data"]["job_id"])
        check("P5 PDF 课程大纲完成", job.get("status") == "ready", str(job))
        pc = get(f"/api/courses/{pdf_course}")["data"]
        plesson = pc["units"][0]["lessons"][0]["id"]

        set_model("mock-lecture")
        r = post(f"/api/courses/lessons/{plesson}/lecture")
        job = wait_job(r["data"]["job_id"])
        check("P6 PDF 讲义完成", job.get("status") == "ready", str(job))
        pl = get(f"/api/courses/lessons/{plesson}")["data"]
        pmarks = pl["board"].get("marks") or []
        check("P7 PDF 讲义产生材料标注", len(pmarks) >= 1, str(len(pmarks)))
        check("P8 标注带真实页码",
              all(m.get("page_no") == 1 for m in pmarks),
              json.dumps(pmarks, ensure_ascii=False))
        check("P9 标注指向该 PDF",
              all(m.get("document_id") == pdf_id for m in pmarks),
              json.dumps(pmarks[:1], ensure_ascii=False))
        check("P10 标注含高亮与圈注",
              {m.get("kind") for m in pmarks} == {"highlight", "circle"},
              str({m.get("kind") for m in pmarks}))

        # 手绘标注：坐标裁剪 + 无材料 id 丢弃
        r = put(f"/api/courses/lessons/{plesson}/marks", {"marks": [
            {"document_id": pdf_id, "page_no": 1, "kind": "highlight",
             "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05, "text": "手绘高亮"},
            {"document_id": pdf_id, "page_no": 1, "kind": "circle",
             "x": 1.4, "y": -0.2, "w": 0.2, "h": 0.1, "text": "坐标应被裁剪"},
            {"document_id": "", "page_no": 3, "kind": "highlight", "text": "缺少材料"},
        ]})
        got = r["data"]["marks"]
        check("P11 标注保存成功", r["code"] == 0, r.get("message", ""))
        check("P12 无材料 id 的标注被丢弃", len(got) == 2, str(len(got)))
        check("P13 越界坐标被裁剪到 [0,1]",
              all(0 <= m["x"] <= 1 and 0 <= m["y"] <= 1 for m in got),
              json.dumps(got, ensure_ascii=False))
        check("P14 标注持久化到讲次",
              len(get(f"/api/courses/lessons/{plesson}")["data"]["marks"]) == 2)

        # 图片题：材料编号 → 真实 document_id / page_no
        set_model("mock-practice")
        r = post(f"/api/courses/lessons/{plesson}/practice", {"count": 5})
        job = wait_job(r["data"]["job_id"])
        check("P15 PDF 出题完成", job.get("status") == "ready", str(job))
        pqs = get(f"/api/courses/lessons/{plesson}/practice")["data"]["items"]
        img_qs = [q for q in pqs if q.get("image")]
        check("P16 生成图片题", len(img_qs) == 1, str(len(img_qs)))
        if img_qs:
            img = img_qs[0]["image"]
            check("P17 图片题指向真实材料页",
                  img.get("document_id") == pdf_id and int(img.get("page_no", 0)) == 1,
                  json.dumps(img, ensure_ascii=False))
            check("P18 图片题给出取图地址",
                  str(img_qs[0].get("image_url", "")).endswith("/raw"),
                  str(img_qs[0].get("image_url")))
        check("P19 列表不泄露答案", all("answer" not in q for q in pqs))

        # 绑定课堂对话后，导出对话应成功（覆盖「有对话」这条路径）
        conv = post("/api/conversations", {"title": "PDF 课堂", "mode": "normal",
                                           "document_ids": [pdf_id]})
        put(f"/api/courses/lessons/{plesson}", {"conversation_id": conv["data"]["id"]})
        with httpx.Client(trust_env=False) as cli:
            r = cli.get(f"{BACKEND}/api/courses/lessons/{plesson}/export?kind=conversation")
            check("P20 绑定对话后可导出", r.status_code == 200 and "PDF 课堂" in r.text,
                  f"{r.status_code} {r.text[:80]}")

        # ── G. 模型不合规时的降级 ───────────────────────
        print("\n[G] 模型不合规 → 确定性兜底")
        set_model("mock-bad-json")
        r = post("/api/courses", {"goal": "再来一门课", "document_ids": [doc_id], "unit_count": 2})
        cid2 = r["data"]["course_id"]
        job = wait_job(r["data"]["job_id"])
        check("G1 非法 JSON 也能出课", job.get("status") == "ready", str(job))
        c2 = get(f"/api/courses/{cid2}")["data"]
        check("G2 课程状态为 ready", c2["status"] == "ready", str(c2.get("error")))
        check("G3 兜底大纲非空", len(c2["units"]) == 2, str(len(c2["units"])))
        l2 = c2["units"][0]["lessons"][0]
        r = post(f"/api/courses/lessons/{l2['id']}/lecture")
        job = wait_job(r["data"]["job_id"])
        check("G4 兜底讲义可用", job.get("status") == "ready", str(job))
        les2 = get(f"/api/courses/lessons/{l2['id']}")["data"]
        check("G5 兜底讲义有卡片", len((les2["board"] or {}).get("cards") or []) >= 1)

        # ── H. 参数校验与错误码 ────────────────────────
        print("\n[H] 参数校验")
        r = post("/api/courses", {"goal": "   ", "document_ids": [doc_id]})
        check("H1 空目标被拒绝", r["code"] != 0, str(r.get("code")))
        r = post("/api/courses", {"document_ids": [doc_id]})
        check("H2 缺目标被拒绝", r["code"] == 1000, str(r.get("code")))
        r = get("/api/courses/not-exist")
        check("H3 不存在的课程 404", r["code"] == 1001, str(r.get("code")))
        r = get("/api/courses/lessons/not-exist")
        check("H4 不存在的讲次 404", r["code"] == 1001, str(r.get("code")))

        # ── H2. 重新生成大纲（带自定义描述）──────────────
        print("\n[H2] 重新生成大纲（自定义描述 note）")
        r = post(f"/api/courses/{cid2}/outline:regenerate",
                 {"note": "单元少一点，只保留 2 个；每个单元多放例题"})
        check("H5 重新生成接受 note", r["code"] == 0, r.get("message", ""))
        job = wait_job(r["data"]["job_id"])
        check("H6 重新生成任务完成", job.get("status") == "ready", str(job))
        c2b = get(f"/api/courses/{cid2}")["data"]
        check("H7 重新生成后仍可用", c2b["status"] == "ready" and len(c2b["units"]) >= 1,
              str(c2b.get("status")))
        # 不存在的课程重新生成 → 404
        r = post("/api/courses/not-exist/outline:regenerate", {"note": "x"})
        check("H8 不存在的课程 404", r["code"] == 1001, str(r.get("code")))

        # ── J. 讲稿不许照念课件 + 单题即时判定 ──────────
        print("\n[J] 讲稿去雷同护栏与单题即时判定")

        def slide_text(sl):
            return "。".join([sl.get("title") or ""] + list(sl.get("bullets") or []))

        # J1-J3：模型把讲稿写成照念课件 → 护栏应触发重写
        set_model("mock-lecture-mirror")
        r = post(f"/api/courses/lessons/{lesson_id}/lecture")
        check("J1 雷同模型下讲义仍生成成功", r["code"] == 0, r.get("message", ""))
        job = wait_job(r["data"]["job_id"])
        check("J2 雷同模型下任务完成", job.get("status") == "ready", str(job))
        les = get(f"/api/courses/lessons/{lesson_id}")["data"]
        slides = les.get("slides") or []
        scripts = les.get("scripts") or []
        check("J3 讲稿与课件逐页对应", len(slides) == len(scripts) and len(slides) >= 3,
              f"slides={len(slides)} scripts={len(scripts)}")
        mirrors = [
            sc for sc in scripts
            if (lambda s: s and s in (sc.get("text") or ""))(
                "".join(ch for ch in slide_text(next(
                    x for x in slides if x["id"] == sc["slide_id"])) if ch.strip()))
        ]
        check("J4 交付出去的讲稿不是照念课件", not mirrors, str([m["slide_id"] for m in mirrors]))
        check("J5 走的是「重写」路径（cue=rewritten）",
              any(sc.get("cue") == "rewritten" for sc in scripts),
              str([sc.get("cue") for sc in scripts]))

        # J6：模型死不改口 → 确定性扩写兜底
        set_model("mock-lecture-stubborn")
        r = post(f"/api/courses/lessons/{lesson_id}/lecture")
        job = wait_job(r["data"]["job_id"])
        check("J6 顽固模型下任务仍完成", job.get("status") == "ready", str(job))
        les = get(f"/api/courses/lessons/{lesson_id}")["data"]
        scripts = les.get("scripts") or []
        check("J7 顽固模型下走确定性扩写（cue=expanded）",
              all(sc.get("cue") == "expanded" for sc in scripts),
              str([sc.get("cue") for sc in scripts]))
        check("J8 扩写文本明显长于课件文字",
              all(len(sc.get("text") or "") > 60 for sc in scripts),
              str([len(sc.get("text") or "") for sc in scripts]))

        # J9-J13：单题即时判定（check）—— 答完即知对错与解析，且不写库
        set_model("mock-practice")
        qs2 = get(f"/api/courses/lessons/{lesson_id}/practice")["data"]["items"]
        bt = {q["type"]: q for q in qs2}
        # 取第一道单选（`bt` 按类型去重会取到最后一道，这里显式取首题）
        q_single = next(q for q in qs2 if q["type"] == "single")
        r = post(f"/api/courses/lessons/{lesson_id}/check",
                 {"question_id": q_single["id"], "answer": 0})
        d = r["data"]
        idx = d.get("expected_index")
        check("J9 check 判定与正确项下标自洽",
              r["code"] == 0 and d["correct"] == (0 == idx), str(d))
        check("J10 check 回传合法选项下标",
              isinstance(idx, int) and 0 <= idx < len(q_single.get("options") or []), str(idx))
        check("J11 check 回传解析文本", bool((d.get("explanation") or "").strip()),
              str(d.get("explanation"))[:60])
        wrong = next((i for i in range(len(q_single.get("options") or [])) if i != idx), None)
        r = post(f"/api/courses/lessons/{lesson_id}/check",
                 {"question_id": q_single["id"], "answer": wrong})
        check("J12 check 判定错误项为错", r["data"]["correct"] is False, str(r["data"]))
        r = post(f"/api/courses/lessons/{lesson_id}/check",
                 {"question_id": "not-exist", "answer": 0})
        check("J13 不存在的题目返回 1001", r["code"] == 1001, str(r.get("code")))

        # check 不写库：连判多次后，正式提交的作答次数只 +1
        set_model("mock-practice")
        set_model("mock-grade")
        r = post(f"/api/courses/lessons/{lesson_id}/grade", {"answers": [
            {"question_id": bt["single"]["id"], "answer": 0},
            {"question_id": bt["boolean"]["id"], "answer": 0},
            {"question_id": bt["fill_in"]["id"], "answer": "0/0 型 与 ∞/∞ 型"},
            {"question_id": bt["open"]["id"], "answer": "先验证类型"},
        ]})
        check("J14 check 不产生额外作答次数（attempt_no 只 +1）",
              r["data"].get("attempt_no") == 3, str(r["data"].get("attempt_no")))

        # ── V. 提示词优化 v1 + 可视化契约 ──────────────
        print("\n[V] 提示词优化 v1 + 可视化契约")
        SPY = DATA / "mock-spy.json"

        def _read_spy() -> list:
            # mock 端是 JSONL 追加写（一次业务动作可能连发多次模型调用），
            # 这里逐行解析后摊平成消息列表。
            for _ in range(20):
                if SPY.exists():
                    try:
                        out: list = []
                        for line in SPY.read_text(encoding="utf-8").splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            msgs = json.loads(line)
                            if isinstance(msgs, list):
                                out.extend(msgs)
                        return out
                    except Exception:
                        pass
                time.sleep(0.3)
            return []

        def _cleanup_courses(*cids: str) -> None:
            with httpx.Client(trust_env=False) as cli:
                for c in cids:
                    cli.delete(f"{BACKEND}/api/courses/{c}", timeout=20)

        # D21 脏 depth 归一到四值枚举
        set_model("mock-outline-dirtydepth")
        r = post("/api/courses", {"goal": "深度归一测试", "document_ids": [doc_id], "unit_count": 2})
        cid_d = r["data"]["course_id"]
        job = wait_job(r["data"]["job_id"])
        depths = [l["depth"] for u in (get(f"/api/courses/{cid_d}")["data"]["units"] or [])
                  for l in u["lessons"]]
        check("D21 脏 depth 归一到四值枚举",
              job.get("status") == "ready"
              and depths and all(d in ("establish", "define", "derive", "apply") for d in depths),
              str(depths))

        # D22 objective 禁词触发重试 → 二轮干净
        set_model("mock-outline-dirty-obj")
        r = post("/api/courses", {"goal": "禁词重试测试", "document_ids": [doc_id], "unit_count": 2})
        cid_o = r["data"]["course_id"]
        job = wait_job(r["data"]["job_id"])
        objs = [l["objective"] for u in (get(f"/api/courses/{cid_o}")["data"]["units"] or [])
                for l in u["lessons"]]
        check("D22 objective 禁词触发重试且二轮干净",
              job.get("status") == "ready" and objs and not any(
                  any(w in o[:12] for w in ("了解", "熟悉", "掌握", "学习")) for o in objs),
              str(objs[:2]))

        # D25 大纲喂料走 overview 路径（prompt 里出现概览段）
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-outline")
        r = post("/api/courses", {"goal": "概览注入测试", "document_ids": [doc_id], "unit_count": 2})
        cid_v = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        spy = _read_spy()
        # 概览文本走 context → 在 user 消息里（build_context 把 outline 置于编号材料之前）
        usr_all = " ".join(str(m.get("content") or "") for m in spy if m.get("role") == "user")
        check("D25 大纲 prompt 注入材料概览段",
              "【材料概览】" in usr_all and "本材料共切分" in usr_all)

        # D23/D24 讲义 prompt：讲法侧重 + 篇幅档双注入；user 消息带【课程信息】
        set_model("mock-spy-lecture")
        lessons_v = [l for u in (get(f"/api/courses/{cid_v}")["data"]["units"] or [])
                     for l in u["lessons"]]
        SPY.unlink(missing_ok=True)
        r = post(f"/api/courses/lessons/{lessons_v[0]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        spy = _read_spy()
        sys_txt = " ".join(str(m.get("content") or "") for m in spy if m.get("role") == "system")
        usr_txt = " ".join(str(m.get("content") or "") for m in spy if m.get("role") == "user")
        check("D23 讲义 prompt 含讲法侧重 + 篇幅档双行",
              "讲法侧重" in sys_txt and "篇幅档" in sys_txt)
        check("D24 讲义 user 消息含【课程信息】块",
              "【课程信息】" in usr_txt and "课程目标" in usr_txt and "学习者水平" in usr_txt)

        # D26 篇幅档随课程 depth 变化（brief vs detailed）
        blocks: dict[str, str] = {}
        for dep in ("brief", "detailed"):
            set_model("mock-outline")
            r = post("/api/courses", {"goal": f"{dep}篇幅对照", "document_ids": [doc_id],
                                      "unit_count": 1, "depth": dep})
            cxd = r["data"]["course_id"]
            wait_job(r["data"]["job_id"])
            ls = [l for u in (get(f"/api/courses/{cxd}")["data"]["units"] or [])
                  for l in u["lessons"]]
            SPY.unlink(missing_ok=True)
            set_model("mock-spy-lecture")
            r = post(f"/api/courses/lessons/{ls[0]['id']}/lecture")
            wait_job(r["data"]["job_id"])
            spy = _read_spy()
            blocks[dep] = " ".join(str(m.get("content") or "") for m in spy
                                   if m.get("role") == "system")
        check("D26 篇幅档随课程 depth 变化",
              "篇幅档·概览" in blocks.get("brief", "")
              and "篇幅档·深入" in blocks.get("detailed", ""),
              str({k: v[:60] for k, v in blocks.items()}))

        # D27 可视化净化：好 diagram/chart 落库（数字转 float），坏字段剥除
        set_model("mock-lecture-viz")
        r = post(f"/api/courses/lessons/{lessons_v[1]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        vslides = get(f"/api/courses/lessons/{lessons_v[1]['id']}")["data"].get("slides") or []
        vjson = json.dumps(vslides, ensure_ascii=False)
        good_diag = next((s for s in vslides if s.get("kind") == "diagram"), None)
        good_chart = next((s for s in vslides if s.get("kind") == "chart"), None)
        check("D27a 合法 diagram 原样落库",
              bool(good_diag) and str((good_diag.get("diagram") or {}).get("code", "")).startswith("flowchart"),
              json.dumps(good_diag or {}, ensure_ascii=False)[:120])
        _cs = ((good_chart or {}).get("chart") or {}).get("series") or []
        _cd = (_cs[0].get("data") if _cs else []) or []
        check("D27b 合法 chart 落库且数字转 float",
              bool(_cd) and all(isinstance(v, float) for v in _cd),
              json.dumps(good_chart or {}, ensure_ascii=False)[:160])
        check("D27c 非法字段被剥除且页面退化为 note",
              "classDef" not in vjson and '"abc"' not in vjson
              and sum(1 for s in vslides if s.get("kind") == "note") >= 2,
              vjson[:200])

        # D28 可视化页 ≤2：三页 diagram 只留两页
        set_model("mock-lecture-viz3")
        r = post(f"/api/courses/lessons/{lessons_v[2]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        s3 = get(f"/api/courses/lessons/{lessons_v[2]['id']}")["data"].get("slides") or []
        check("D28 可视化页超限被剥到 ≤2",
              sum(1 for s in s3 if s.get("kind") == "diagram") == 2,
              str([s.get("kind") for s in s3]))

        # D29/D30 学习目标推荐改喂「材料概览」（原走空泛语义检索：
        # query「材料主题与核心内容」词汇零交集 → 0 命中 → 概览只取开头 3 片 →
        # 模型面对近乎空白的上下文幻觉出通用 AI 课目标。用户实测：勾文言文材料
        # 却推荐出「大模型/部署/量化」目标）。
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-goals")
        r = post("/api/courses/suggest-goals", {"document_ids": [doc_id]})
        spy = _read_spy()
        usr_all = " ".join(str(m.get("content") or "") for m in spy if m.get("role") == "user")
        check("D29 推荐喂料是材料概览且未越界到未勾选文档",
              "【材料概览】" in usr_all and "洛必达" in usr_all
              and "calculus" not in usr_all.lower(),
              usr_all[:200])
        srcs = (r.get("data") or {}).get("sources") or []
        check("D30 推荐结果回传材料来源（供 UI 显示，错配一眼可见）",
              len(srcs) == 1 and "高数" in srcs[0], str(srcs))

        # D31–D34 P1 特色页契约：对比表格（计入可视化页上限）/ 金句卡（不计入、限 1 页）
        set_model("mock-lecture-p1")
        r = post(f"/api/courses/lessons/{lessons_v[3]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        p1 = get(f"/api/courses/lessons/{lessons_v[3]['id']}")["data"].get("slides") or []
        good_tbl = next((s for s in p1 if s.get("kind") == "table"), None)
        gtab = (good_tbl or {}).get("table") or {}
        check("D31 合法对比表格落库（列名与行数完整）",
              bool(good_tbl) and gtab.get("columns") == ["项目", "936", "65001"]
              and len(gtab.get("rows") or []) == 2,
              json.dumps(good_tbl or {}, ensure_ascii=False)[:180])
        bad_page = next((s for s in p1 if "坏表格" in str(s.get("title") or "")), None)
        check("D32 列数不齐的坏表格被剥除且页面退化为 note",
              bool(bad_page) and bad_page.get("kind") == "note" and "table" not in bad_page,
              json.dumps(bad_page or {}, ensure_ascii=False)[:160])
        tk = next((s for s in p1 if s.get("kind") == "takeaway"), None)
        tk_text = str((tk or {}).get("takeaway") or "")
        check("D33 金句卡落库且超长被截断到 ≤60 字",
              bool(tk) and 0 < len(tk_text) <= 60
              and len((tk or {}).get("title") or "") > 0,
              f"len={len(tk_text)} text={tk_text[:40]}")
        check("D34 金句卡超页数上限被剥到 1 页",
              sum(1 for s in p1 if s.get("kind") == "takeaway") == 1
              and not any("第二句金句" in str(s.get("takeaway") or "") for s in p1),
              str([s.get("kind") for s in p1]))

        _cleanup_courses(cid_d, cid_o, cid_v)

        # ── H. hands_on 实操题与课程级「实践环节」开关 ──
        print("\n[H] hands_on 实操题与课程级实践开关")

        r = post("/api/courses", {"goal": "实操开关默认值", "document_ids": [doc_id],
                                  "unit_count": 1})
        cid_on = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        c_on = get(f"/api/courses/{cid_on}")["data"]
        check("K1 不传 hands_on → 「自动」三态（存 NULL，回显 null）",
              c_on.get("hands_on") is None, str(c_on.get("hands_on")))

        # K1b 三态的另两端：显式 true（含实操）/ false（纯理论）
        r = post("/api/courses", {"goal": "显式开启实操", "document_ids": [doc_id],
                                  "unit_count": 1, "hands_on": True})
        cid_true = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        c_true = get(f"/api/courses/{cid_true}")["data"]
        check("K1b 显式传 hands_on=true → 回显 true（三态的「含实操」端）",
              c_true.get("hands_on") is True, str(c_true.get("hands_on")))

        r = post("/api/courses", {"goal": "纯理论课程", "document_ids": [doc_id],
                                  "unit_count": 1, "hands_on": False})
        cid_off = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        c_off = get(f"/api/courses/{cid_off}")["data"]
        check("K2 可显式关闭 hands_on（纯理论课）",
              c_off.get("hands_on") is False, str(c_off.get("hands_on")))

        les_on = [l for u in c_on["units"] for l in u["lessons"] if l["kind"] == "lecture"]
        les_off = [l for u in c_off["units"] for l in u["lessons"] if l["kind"] == "lecture"]

        set_model("mock-practice-hands")
        r = post(f"/api/courses/lessons/{les_on[0]['id']}/practice", {"count": 5})
        wait_job(r["data"]["job_id"])
        qs_on = get(f"/api/courses/lessons/{les_on[0]['id']}/practice")["data"]["items"]
        check("K3 开启实操的课程会出 hands_on 题",
              any(q["type"] == "hands_on" for q in qs_on),
              str([q["type"] for q in qs_on]))

        r = post(f"/api/courses/lessons/{les_off[0]['id']}/practice", {"count": 5})
        wait_job(r["data"]["job_id"])
        qs_off = get(f"/api/courses/lessons/{les_off[0]['id']}/practice")["data"]["items"]
        check("K4 关闭实操的课程不含 hands_on 题（提示词禁止 + 服务端兜底剥除）",
              qs_off and not any(q["type"] == "hands_on" for q in qs_off),
              str([q["type"] for q in qs_off]))

        ho = next((q for q in qs_on if q["type"] == "hands_on"), None)
        set_model("mock-grade")
        r = post(f"/api/courses/lessons/{les_on[0]['id']}/grade", {"answers": [
            {"question_id": (ho or {}).get("id"), "answer": "936"}]})
        ok_res = next((x for x in r["data"]["results"] if x["type"] == "hands_on"), {})
        r2 = post(f"/api/courses/lessons/{les_on[0]['id']}/grade", {"answers": [
            {"question_id": (ho or {}).get("id"), "answer": "完全答不对的内容"}]})
        bad_res = next((x for x in r2["data"]["results"] if x["type"] == "hands_on"), {})
        check("K5 实操题复用可接受答案数组判分（936 对 / 乱填错）",
              bool(ho) and ok_res.get("correct") is True and bad_res.get("correct") is False,
              f"{ok_res.get('correct')} / {bad_res.get('correct')}")

        # H6 纯理论课：讲义 prompt 必须禁止布置真实操作任务
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-lecture")
        r = post(f"/api/courses/lessons/{les_off[0]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        spy = _read_spy()
        sys_txt = " ".join(str(m.get("content") or "") for m in spy if m.get("role") == "system")
        check("K6 纯理论课的讲义 prompt 禁止布置操作任务",
              "不得布置真实操作任务" in sys_txt, sys_txt[-180:])

        # H7 课程列表也回显该字段（列表页/回退逻辑都依赖同一 _course_out）
        cs = get("/api/courses")["data"]
        items = cs.get("items", cs) if isinstance(cs, dict) else cs
        hit = next((x for x in items if x.get("id") == cid_off), None)
        check("K7 课程列表同样回显 hands_on",
              bool(hit) and hit.get("hands_on") is False,
              str(hit.get("hands_on") if hit else None))

        # K8/K9 单元数量：自定义输入的上限是 12（前端 1~12 + 后端 Pydantic le=12）
        set_model("mock-outline")
        r12 = post("/api/courses", {"goal": "单元数上限 12", "document_ids": [doc_id],
                                    "unit_count": 12})
        cid_u12 = r12["data"]["course_id"]
        wait_job(r12["data"]["job_id"])
        c12 = get(f"/api/courses/{cid_u12}")["data"]
        check("K8 unit_count=12 接受并落库（「自定义」的上限）",
              c12.get("unit_count") == 12, str(c12.get("unit_count")))
        r13 = post("/api/courses", {"goal": "单元数超限", "document_ids": [doc_id],
                                    "unit_count": 13})
        check("K9 unit_count=13 被拒（上限 12，不放过误填）",
              "course_id" not in (r13.get("data") or {}), str(r13)[:140])

        _cleanup_courses(cid_on, cid_off, cid_true, cid_u12)

        # ── X. 架构化图示：typed IR → 后端确定性编译 ──
        print("\n[X] 架构化图示（IR → 确定性编译 SVG）")
        # 注意：[V] 组末尾已经清理掉它建的那门课，这里必须自己新建一门再取讲次
        set_model("mock-outline")
        _rx = post("/api/courses", {"goal": "架构化图示测试", "document_ids": [doc_id],
                                   "unit_count": 2})
        cid_x = _rx["data"]["course_id"]
        wait_job(_rx["data"]["job_id"])
        lessons_x = [l for u in (get(f"/api/courses/{cid_x}")["data"]["units"] or [])
                     for l in u["lessons"]]
        lec_ids = [l["id"] for l in lessons_x if str(l.get("kind")) == "lecture"]
        check("X.a 取到可用的讲义讲次", len(lec_ids) >= 2,
              str([l.get("kind") for l in lessons_x]))
        lic = lec_ids[-1]

        set_model("mock-lecture-ir")
        r = post(f"/api/courses/lessons/{lic}/lecture")
        check("X0 图示讲义任务已创建", r.get("code") == 0 and bool(r.get("data")), str(r)[:200])
        wait_job(r["data"]["job_id"])
        sl = get(f"/api/courses/lessons/{lic}")["data"].get("slides") or []
        dg = next((s for s in sl if s.get("kind") == "diagram"), None)
        dgv = (dg or {}).get("diagram") or {}
        svg = dgv.get("svg") if isinstance(dgv.get("svg"), str) else ""
        check("X1 图示页落库带后端编译好的 SVG",
              bool(dg) and svg.lstrip().startswith("<svg"), json.dumps(dgv, ensure_ascii=False)[:140])
        check("X2 SVG 内含真实节点与关系",
              svg.count('class="zf-node"') >= 3 and svg.count('class="zf-edge"') >= 2,
              f"nodes={svg.count('zf-node')} edges={svg.count('zf-edge')}")
        check("X3 原始 IR 一并保留（可追溯、可重编译）",
              isinstance(dgv.get("ir"), dict) and dgv["ir"].get("diagram_type") == "workflow")
        check("X4 图型与预设随页落库",
              dgv.get("diagram_type") == "workflow" and isinstance(dgv.get("preset"), str))

        # 坏 IR → 带回执重写一次 → 编译成功（页面仍是图示页）
        set_model("mock-lecture-ir-bad")
        r = post(f"/api/courses/lessons/{lic}/lecture")
        wait_job(r["data"]["job_id"])
        sl2 = get(f"/api/courses/lessons/{lic}")["data"].get("slides") or []
        dg2 = next((s for s in sl2 if s.get("kind") == "diagram"), None)
        svg2 = ((dg2 or {}).get("diagram") or {}).get("svg") or ""
        check("X5 坏 IR 经回执重写后编译成功（页面保住为图示页）",
              bool(dg2) and isinstance(svg2, str) and svg2.lstrip().startswith("<svg"),
              json.dumps((dg2 or {}).get("diagram") or {}, ensure_ascii=False)[:140])

        # 顽固坏 IR → 退化要点页（永不空白）
        set_model("mock-lecture-ir-stubborn")
        r = post(f"/api/courses/lessons/{lic}/lecture")
        wait_job(r["data"]["job_id"])
        sl3 = get(f"/api/courses/lessons/{lic}")["data"].get("slides") or []
        bad_page = next((s for s in sl3 if str(s.get("title") or "") == "chcp 设置流程"), None)
        check("X6 重写仍不合格 → 退化为要点页（kind=note 且无 diagram 字段）",
              bool(bad_page) and bad_page.get("kind") == "note" and "diagram" not in bad_page,
              json.dumps(bad_page or {}, ensure_ascii=False)[:160])
        check("X7 退化后学生看到的是要点（不是空白/报错）",
              bool(bad_page) and bool(bad_page.get("bullets")))

        # 旧 Mermaid 形态仍保留（历史讲义兼容）
        set_model("mock-lecture-viz")
        r = post(f"/api/courses/lessons/{lec_ids[0]}/lecture")
        wait_job(r["data"]["job_id"])
        _lid_legacy = lec_ids[0]
        vsl = get(f"/api/courses/lessons/{_lid_legacy}")["data"].get("slides") or []
        legacy_dg = next((s for s in vsl if s.get("kind") == "diagram"), None)
        check("X8 旧 Mermaid 形态仍保留（向后兼容历史讲义）",
              bool(legacy_dg) and isinstance((legacy_dg.get("diagram") or {}).get("code"), str)
              and not (legacy_dg.get("diagram") or {}).get("svg"),
              json.dumps((legacy_dg or {}).get("diagram") or {}, ensure_ascii=False)[:140])

        _cleanup_courses(cid_x)

        # ── Y. 图示硬约束 + 补图兜底 + 篇幅下限 + 单元讲次数弹性 ──
        print("\n[Y] 图示硬约束与补图兜底")

        # Y0 补图兜底是**按材料内容**决定要不要触发的：
        # 主测试材料（极限/洛必达）措辞里没有「步骤/对照/状态/层级」这类结构信号，
        # 对它不补图才是正确行为（见 diagram_ir_check 6.2/6.3）。所以这里另配一份
        # 明确含流程与对照的材料，用来验证「该补的时候真的会补」。
        with httpx.Client(trust_env=False) as _cli:
            _md = (
                "# 编码问题排查流程与做法对照\n\n"
                "## 排查步骤\n\n"
                "第一步：确认文件保存的编码类型。第二步：检查命令行当前的活动代码页。"
                "第三步：对比两者是否一致，不一致就会出现乱码。这个顺序不能颠倒。\n\n"
                "## 两种做法对照\n\n"
                "直接双击 .bat 的做法不安全；改为拖拽传参的做法安全。"
                "两种做法的区别在于是否需要命令行解析中文，输入内容与输出内容是否一致。\n\n"
                "## 状态切换\n\n"
                "当代码页在不匹配的状态之间切换时，中文字节会被错误转换并变成乱码。\n\n"
                "## 结构层级\n\n"
                "从文件编码到控制台代码页再到字体，属于三个不同的层级，"
                "任何一层不匹配都会出错，所以要按顺序逐层排查。\n"
            ).encode("utf-8")
            _r = _cli.post(f"{BACKEND}/api/documents/upload",
                           files={"files": ("编码排查流程与对照.md", _md, "text/markdown")},
                           timeout=60).json()
            doc_vis = _r["data"]["documents"][0]["id"]
            _st = ""
            for _ in range(80):
                _st = _cli.get(f"{BACKEND}/api/documents/{doc_vis}", timeout=10
                               ).json()["data"]["document"]["status"]
                if _st in ("ready", "failed"):
                    break
                time.sleep(0.5)
        check("Y0 可视化材料上传并解析完成", _st == "ready", _st)

        # Y1 大纲提示词实况：讲次数改由内容体量决定
        # （旧版写死「每个单元 2~4 个讲次」，模型为了凑数会把一讲的内容拆薄 → 每讲仅 7~10 页）
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-outline")
        r = post("/api/courses", {"goal": "提示词实况核对", "document_ids": [doc_vis],
                                  "unit_count": 2, "depth": "standard"})
        cid_y1 = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        spy = _read_spy()
        sys_outline = " ".join(str(m.get("content") or "") for m in spy
                               if m.get("role") == "system")
        check("Y1 大纲提示词：讲次数由内容体量决定（不再写死 2~4）",
              "由该单元的内容体量决定" in sys_outline
              and "每个单元 2~4 个讲次" not in sys_outline
              and "严禁为了凑数量" in sys_outline,
              sys_outline[sys_outline.find("每个单元的讲次数"):][:120] or "未找到该规则")

        # Y2 讲义提示词实况：图示硬要求 + 页数下限
        lessons_y1 = [l for u in (get(f"/api/courses/{cid_y1}")["data"]["units"] or [])
                      for l in u["lessons"] if l.get("kind") == "lecture"]
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-lecture")
        r = post(f"/api/courses/lessons/{lessons_y1[0]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        spy = _read_spy()
        sys_lec = " ".join(str(m.get("content") or "") for m in spy if m.get("role") == "system")
        check("Y2 讲义提示词：图示硬要求 + 篇幅下限（standard=少于 12 页不合格）",
              "至少安排 1 页 diagram" in sys_lec
              and "本讲若涉及" in sys_lec
              and "少于 12 页即不合格" in sys_lec,
              "缺硬要求" if "至少安排 1 页 diagram" not in sys_lec
              else ("缺页数下限" if "少于 12 页即不合格" not in sys_lec else ""))

        # Y3–Y6 补图兜底：整讲无可视化 → 服务端自动补 1 页
        set_model("mock-lecture-plain")
        r = post(f"/api/courses/lessons/{lessons_y1[0]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        det = get(f"/api/courses/lessons/{lessons_y1[0]['id']}")["data"]
        ysl = det.get("slides") or []
        ysc = det.get("scripts") or []
        viz = [x for x in ysl if x.get("diagram") or x.get("chart") or x.get("table")]
        check("Y3 整讲无可视化时服务端自动补 1 页", len(viz) == 1,
              f"页数={len(ysl)} 可视化={len(viz)} kinds={[x.get('kind') for x in ysl]}")
        check("Y4 补的图走了 Archify 校验链（是编译后的 SVG，不是裸 IR）",
              bool(viz) and str((viz[0].get("diagram") or {}).get("svg") or ""
                                ).lstrip().startswith("<svg"),
              json.dumps((viz[0].get("diagram") or {}), ensure_ascii=False)[:160] if viz else "无")
        check("Y5 补图后课件页与讲稿仍一一对应", len(ysl) == len(ysc) and len(ysl) == 4,
              f"slides={len(ysl)} scripts={len(ysc)}（原 3 页 + 补 1 页 = 4）")
        check("Y6 补图页插在小结之前（不是直接追加到末尾）",
              bool(viz) and bool(ysl) and ysl[-1].get("id") != viz[0].get("id"),
              f"末尾={ysl[-1].get('id') if ysl else None} 补图={viz[0].get('id') if viz else None}")

        # Y7–Y8 补图退化路径：模型**没给这一页讲稿**时，服务端必须用确定性方法补一版，
        # 让「课件页 ⇄ 讲稿」仍然严格一一对应 —— 否则多出来的那页课件没有讲稿，
        # 导出 / Markdown 的「第 i 页 · 讲稿」会整体错位（前端能降级显示，
        # 但落库 JSON 本身是残的）。
        set_model("mock-lecture-plain-noscript")
        r = post(f"/api/courses/lessons/{lessons_y1[0]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        det = get(f"/api/courses/lessons/{lessons_y1[0]['id']}")["data"]
        ysl = det.get("slides") or []
        ysc = det.get("scripts") or []
        viz = [x for x in ysl if x.get("diagram") or x.get("chart") or x.get("table")]
        check("Y7 补图未给讲稿时仍补出 1 页可视化", len(viz) == 1, f"可视化={len(viz)}")
        check("Y8 补图页讲稿由服务端确定性补齐（页数 == 讲稿数 == 4）",
              len(ysl) == len(ysc) and len(ysl) == 4,
              f"slides={len(ysl)} scripts={len(ysc)}")
        check("Y8b 补出来的那一页在讲稿里有同名 slide_id（严格配对）",
              bool(viz) and any(s.get("slide_id") == viz[0].get("id") for s in ysc),
              f"补图 id={viz[0].get('id') if viz else None} "
              f"scripts={[s.get('slide_id') for s in ysc]}")

        # ── Z. 讲次教学设计 desc（大纲详细说明注入逐讲生成）──
        print("\n[Z] 讲次教学设计 desc")

        # Z1 讲义提示词实况：desc 注入【本讲教学设计】（cid_y1 的 mock 大纲带 desc）
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-lecture")
        r = post(f"/api/courses/lessons/{lessons_y1[0]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        spy = _read_spy()
        sys_z1 = " ".join(str(m.get("content") or "") for m in spy if m.get("role") == "system")
        check("Z1 讲义提示词：desc 注入【本讲教学设计】（边界/术语口径/衔接都在）",
              "【本讲教学设计】" in sys_z1
              and "知识点边界" in sys_z1
              and "术语口径" in sys_z1
              and "避免展开" in sys_z1,
              sys_z1[sys_z1.find("【本讲教学设计"):][:120] if "【本讲教学设计" in sys_z1
              else "提示词中未找到教学设计块")

        # Z2 练习提示词实况：练习讲专用 desc 字段（考察点/易错点/题型安排）
        prac_y1 = next(l for u in (get(f"/api/courses/{cid_y1}")["data"]["units"] or [])
                       for l in u["lessons"] if l.get("kind") == "practice")
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-lecture")   # 回讲义桩：练习校验不过走兜底，但提示词已落盘
        r = post(f"/api/courses/lessons/{prac_y1['id']}/practice")
        wait_job(r["data"]["job_id"])
        spy = _read_spy()
        sys_z2 = " ".join(str(m.get("content") or "") for m in spy if m.get("role") == "system")
        check("Z2 练习提示词：练习讲 desc（考察点/易错点/题型安排）注入，且不带正文讲字段",
              "【本讲教学设计】" in sys_z2
              and "考察点" in sys_z2
              and "学生易错点" in sys_z2
              and "题型安排" in sys_z2
              and "知识点边界" not in sys_z2,
              sys_z2[sys_z2.find("【本讲教学设计"):][:120] if "【本讲教学设计" in sys_z2
              else "提示词中未找到教学设计块")

        # Z5 desc 展示到大纲预览：课程详情 API 的讲次输出带 desc 字段
        det_y = get(f"/api/courses/{cid_y1}")["data"]
        _l0 = det_y["units"][0]["lessons"][0]
        check("Z5 课程详情 API 输出讲次 desc（大纲预览展示用）",
              isinstance(_l0.get("desc"), dict)
              and bool(_l0["desc"].get("knowledge_points")),
              json.dumps(_l0.get("desc"), ensure_ascii=False)[:100])

        # Z3 大纲对齐校验真的发起了：同一次出纲里 spy 应同时落盘
        # 大纲调用与「课程审校」对齐校验调用（JSONL 两行）
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-outline")
        r = post("/api/courses", {"goal": "对齐校验提示词实况", "document_ids": [doc_id],
                                  "unit_count": 2, "depth": "standard"})
        cid_z3 = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        spy = _read_spy()
        sys_z3 = [str(m.get("content") or "") for m in spy if m.get("role") == "system"]
        check("Z3 大纲对齐校验已接入出纲链路（审校调用与大纲调用先后落盘）",
              any("你是课程审校" in s for s in sys_z3)
              and any("每个单元的讲次数" in s for s in sys_z3),
              f"spy 落盘 system 消息 {len(sys_z3)} 条；"
              f"审校={'有' if any('课程审校' in s for s in sys_z3) else '无'}")
        _cleanup_courses(cid_z3)

        # Z4 向后兼容：旧大纲（无 desc）生成的讲义提示词不含教学设计块，行为与旧版一致
        set_model("mock-outline-5")
        r = post("/api/courses", {"goal": "无 desc 向后兼容", "document_ids": [doc_id],
                                  "unit_count": 5, "depth": "standard"})
        cid_z4 = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        lec_z4 = [l for u in (get(f"/api/courses/{cid_z4}")["data"]["units"] or [])
                  for l in u["lessons"] if l.get("kind") == "lecture"]
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-lecture")
        r = post(f"/api/courses/lessons/{lec_z4[0]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        spy = _read_spy()
        sys_z4 = " ".join(str(m.get("content") or "") for m in spy if m.get("role") == "system")
        det_z4 = get(f"/api/courses/lessons/{lec_z4[0]['id']}")["data"]
        check("Z4 旧大纲（无 desc）讲义正常生成且提示词不含教学设计块",
              "【本讲教学设计】" not in sys_z4
              and bool(det_z4.get("slides"))
              and len(det_z4.get("slides") or []) == len(det_z4.get("scripts") or []),
              f"slides={len(det_z4.get('slides') or [])} "
              f"含教学设计块={'是' if '【本讲教学设计】' in sys_z4 else '否'}")

        _cleanup_courses(cid_z4)

        # Z6 旧课程「补写教学设计」：只补 desc，**不动结构、不动已生成的讲义**。
        # 背景：desc 功能上线前建的课 desc_json 全为 NULL；用户若走「重新生成大纲」
        # 会重建讲次、把已经看过/听过的讲义一起丢掉。
        set_model("mock-outline-5")            # 无 desc 的旧大纲桩
        r = post("/api/courses", {"goal": "补写教学设计端到端", "document_ids": [doc_id],
                                  "unit_count": 5, "depth": "standard"})
        cid_z6 = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        lessons_z6 = [l for u in (get(f"/api/courses/{cid_z6}")["data"]["units"] or [])
                      for l in u["lessons"]]
        titles_before = [l["title"] for l in lessons_z6]
        check("Z6a 初始状态：旧课讲次 desc 全为空",
              bool(lessons_z6) and all(not l.get("desc") for l in lessons_z6),
              f"讲次={len(lessons_z6)} 有 desc 的={sum(1 for l in lessons_z6 if l.get('desc'))}")

        # 先给第一讲生成讲义 —— 补写 desc 后它必须原样还在
        lec_z6 = [l for l in lessons_z6 if l.get("kind") == "lecture"]
        set_model("mock-spy-lecture")
        r = post(f"/api/courses/lessons/{lec_z6[0]['id']}/lecture")
        wait_job(r["data"]["job_id"])
        slides_before = len(get(f"/api/courses/lessons/{lec_z6[0]['id']}")["data"].get("slides") or [])

        r = post(f"/api/courses/{cid_z6}/desc:rebuild")
        wait_job(r["data"]["job_id"])
        lessons_after = [l for u in (get(f"/api/courses/{cid_z6}")["data"]["units"] or [])
                         for l in u["lessons"]]
        lect = [l for l in lessons_after if l.get("kind") == "lecture" and l.get("desc")]
        prac = [l for l in lessons_after if l.get("kind") == "practice" and l.get("desc")]
        check("Z6b 补写后每讲都有 desc，且结构未变（标题序列一致、数量一致）",
              len(lessons_after) == len(lessons_z6)
              and all(l.get("desc") for l in lessons_after)
              and [l["title"] for l in lessons_after] == titles_before,
              f"补写={sum(1 for l in lessons_after if l.get('desc'))}/{len(lessons_after)} "
              f"结构一致={[l['title'] for l in lessons_after] == titles_before}")
        check("Z6c 正文讲 desc 带知识边界/术语口径，练习讲用专用字段",
              bool(lect) and all(d.get("knowledge_points") and d.get("concepts")
                                 for d in (l["desc"] for l in lect))
              and bool(prac) and all(l["desc"].get("exercise_focus") for l in prac),
              f"正文讲={len(lect)} 练习讲={len(prac)}")
        slides_after = len(get(f"/api/courses/lessons/{lec_z6[0]['id']}")["data"].get("slides") or [])
        check("Z6d 补写 desc 不影响已生成的讲义（页数不变）",
              slides_before > 0 and slides_after == slides_before,
              f"{slides_before} → {slides_after}")

        _cleanup_courses(cid_z6)

# Z7 编辑结构后的内容归属：内容字段必须跟着讲次**对象**走，不能跟着位置走。
        # 场景＝用户实测：在「编辑课程结构」里删掉第 1 讲、末尾再加 2 讲。
        # 新前端提交带讲次 id，后端按 id 复用行 → 被删讲次之后的内容错位问题根除。
        set_model("mock-outline-5")
        r = post("/api/courses", {"goal": "编辑结构后内容归属", "document_ids": [doc_id],
                                  "unit_count": 5, "depth": "standard"})
        cid_z7 = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        r = post(f"/api/courses/{cid_z7}/desc:rebuild")
        wait_job(r["data"]["job_id"])

        def _z7_payload(data: dict, with_id: bool = True) -> list:
            """模拟前端确认页提交：
            ``with_id=True`` 时带上讲次 id（新前端格式）；``False`` 模拟老客户端（无 id）。
            每个讲次**带着自己的** desc/depth，**不动内容字段**。"""
            out = []
            for u in data["units"]:
                lessons = []
                for l in u["lessons"]:
                    item = {"title": l["title"], "objective": l["objective"],
                            "kind": l["kind"], "depth": l["depth"],
                            "desc": l.get("desc")}
                    if with_id:
                        item["id"] = l.get("id") or None
                    lessons.append(item)
                out.append({"title": u["title"], "summary": u.get("summary") or "",
                            "lessons": lessons})
            return out

        d7 = get(f"/api/courses/{cid_z7}")["data"]
        orig = list(d7["units"][0]["lessons"])
        # 给**会保留**的讲次生成讲义，记录 slides 数，验证「内容跟着讲次走」。
        # 注意：Z7 场景会删掉第 1 讲（orig[0]），所以 target 必须选原第 2 讲起的一个 lecture，
        # 否则删完再查它 → 404（内容已随 id 删除，无法追踪归属）。
        target7 = next((l for l in orig[1:] if l["kind"] == "lecture"), None)
        check("Z7-0 目标讲次讲义已生成（有 slides 可追踪）",
              target7 is not None, f"orig{len(orig)} 讲中无可用 lecture")
        if target7 is None:
            _cleanup_courses(cid_z7)
            raise SystemExit("Z7 前置条件不满足：无可用 target lecture")
        set_model("mock-spy-lecture")
        r = post(f"/api/courses/lessons/{target7['id']}/lecture")
        wait_job(r["data"]["job_id"])
        target7_slides = len(
            get(f"/api/courses/lessons/{target7['id']}")["data"].get("slides") or [])
        check("Z7-1 目标讲次讲义已生成（有 slides 可追踪）",
              target7_slides > 0, f"slides={target7_slides}")

        units7 = _z7_payload(d7, with_id=True)
        units7[0]["lessons"] = units7[0]["lessons"][1:]        # 删掉第 1 讲
        units7[0]["lessons"] += [                               # 末尾加 2 讲
            {"id": None, "title": "新增讲次甲", "objective": "能说明甲", "kind": "lecture",
             "depth": "standard", "desc": None},
            {"id": None, "title": "新增讲次乙", "objective": "能说明乙", "kind": "lecture",
             "depth": "standard", "desc": None},
        ]
        post(f"/api/courses/{cid_z7}/outline:confirm", {"title": d7["title"], "units": units7})
        after = get(f"/api/courses/{cid_z7}")["data"]["units"][0]["lessons"]
        # 补写桩把讲次标题写进 outcomes（「能说明「<标题>」的要点」）→ 可用它判定 desc 归属
        lect7 = [l for l in after if l["kind"] == "lecture" and l.get("desc")]
        mismatched = [l["title"] for l in lect7
                      if l["title"][:14] not in " ".join(l["desc"].get("outcomes") or [])]
        check("Z7a 编辑结构后保留讲次的 desc 仍指向自己（未错位到相邻讲）",
              bool(lect7) and not mismatched, f"错位={mismatched}")
        leaked = [l["title"] for l in after if l.get("desc") and
                  orig[0]["title"][:14] in " ".join(l["desc"].get("outcomes") or [])]
        check("Z7b 被删讲次的 desc 没有残留到别的讲次上", not leaked, f"残留={leaked}")
        added7 = [l for l in after if l["title"].startswith("新增讲次")]
        check("Z7c 新增的讲次 desc 为空（等用户补写，不继承别人的）",
              len(added7) == 2 and all(not l.get("desc") for l in added7),
              f"新增={len(added7)} 有 desc={sum(1 for l in added7 if l.get('desc'))}")
        check("Z7d 讲次数 = 原 -1 +2",
              len(after) == len(orig) - 1 + 2, f"{len(orig)} → {len(after)}")
        # 核心：目标讲次（原第 2 讲，被删第 1 讲后上移一位）的标题与它有 slides 的
        # 事实仍匹配 —— 讲义内容跟着它的 id 走了，不再按位置错位到新讲次。
        tgt_after = next((l for l in after if l["id"] == target7["id"]), None)
        tgt_slides_now = len(
            get(f"/api/courses/lessons/{target7['id']}")["data"].get("slides") or [])
        check("Z7 内容归属：目标讲次标题与有 slides 的事实仍匹配（内容跟着 id 走）",
              tgt_after is not None
              and tgt_after["title"] == target7["title"]
              and tgt_slides_now == target7_slides,
              f"目标={target7['title']} slides={tgt_slides_now}")
        check("Z7 新增讲次 slides 为空（内容不继承被删讲次）",
              all(len(get(f"/api/courses/lessons/{l['id']}")["data"].get("slides") or []) == 0
                  for l in added7),
              f"新增={[len(get(f'/api/courses/lessons/{l['id']}')['data'].get('slides') or []) for l in added7]}")
        # 被删讲次（第 1 讲）的内容随之消失：其 id 已不存在于新结构中
        check("Z7 被删讲次的讲义随之消失",
              all(l["id"] != orig[0]["id"] for l in after)
              and get(f"/api/courses/lessons/{orig[0]['id']}")["code"] == 1001,
              f"被删讲次 id={orig[0]['id']}")

        # Z7e 兼容性：不带 id（模拟老客户端）→ 行为与旧版一致（按位置复用行）。
        # desc 一并提交的情况下，位置复用仍能满足归属（因为顺序没变）。
        set_model("mock-outline-5")
        units8 = _z7_payload(get(f"/api/courses/{cid_z7}")["data"], with_id=False)
        post(f"/api/courses/{cid_z7}/outline:confirm", {"title": d7["title"], "units": units8})
        keep = get(f"/api/courses/{cid_z7}")["data"]["units"][0]["lessons"]
        before_n = sum(1 for l in after if l.get("desc"))
        check("Z7e 不带 id 提交：行为与旧版一致（按位置复用行）",
              sum(1 for l in keep if l.get("desc")) == before_n
              and [l["title"] for l in keep] == [l["title"] for l in units8[0]["lessons"]],
              f"desc前={before_n} 后={sum(1 for l in keep if l.get('desc'))}")
        _cleanup_courses(cid_z7)

        # Z8 课型（学习意图）：主课型决定大纲结构 —— 取代原来「4 条能力目标单选」。
        # 用户的原始反馈：那 4 条其实是**同一门课的 4 个侧面**，做成单选本身就别扭；
        # 而「想要什么形态的课」才是用户真正要表达的东西。
        cat = get("/api/courses/intents")["data"]["items"]
        ids_live = [t["id"] for t in cat]
        check("Z8a 课型接口返回全部已上线课型（通用 5 + 语文 3 + 对比阅读 / 微课）",
              ids_live == ["overview", "deep-read", "exam", "inquiry", "project",
                           "recite", "character", "culture", "contrast", "micro"],
              f"got={ids_live}")

        set_model("mock-normal")
        d8 = post("/api/courses/suggest-intents", {"document_ids": [doc_id]})["data"]
        check("Z8b 按材料推荐课型：首选与备选都必须是已上线 id，且备选不含首选",
              d8.get("primary") in ids_live and bool(d8.get("alternatives"))
              and all(a in ids_live and a != d8["primary"] for a in d8["alternatives"]),
              f"{d8}")

        g8 = str(post("/api/courses/suggest-goal", {
            "document_ids": [doc_id], "primary": d8["primary"]})["data"].get("goal") or "")
        check("Z8c 按课型写的是「一句目的」，不是「能…能…能…」的能力清单",
              bool(g8) and g8.count("能") <= 1, f"goal={g8[:60]}")

        # Z8d 建课带课型、goal 故意留空 → 后端按课型兜底；详情回带课型名
        SPY.unlink(missing_ok=True)
        set_model("mock-spy-outline")
        r = post("/api/courses", {
            "goal": "", "document_ids": [doc_id], "unit_count": 5, "depth": "standard",
            "intent": {"primary": "deep-read", "assist": "exam", "note": "学生初三"}})
        cid_z8 = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        c8 = get(f"/api/courses/{cid_z8}")["data"]
        it8 = c8.get("intent") or {}
        check("Z8d 目标留空时按课型兜底（课程 goal 非空）",
              bool(str(c8.get("goal") or "").strip()), f"goal={c8.get('goal')}")
        check("Z8e 课程详情回带课型（主/辅名称 + 补充都带上）",
              it8.get("primary_name") == "由浅入深精读型"
              and it8.get("assist_name") == "考点应试型" and it8.get("note") == "学生初三",
              f"intent={it8}")
        spy8 = " ".join(str(m.get("content") or "") for m in _read_spy()
                        if m.get("role") == "system")
        check("Z8f 出纲提示词含课型推进顺序 + 辅助课型加强项 + 用户补充",
              "【本次课型" in spy8 and "整体概览 → 背景与人物" in spy8
              and "辅助课型：考点应试型" in spy8 and "学生初三" in spy8,
              f"含课型块={'【本次课型' in spy8}")
        _cleanup_courses(cid_z8)

        # Z8g 兼容：不传 intent（旧客户端）→ 提示词里没有课型块，行为与旧版一致
        SPY.unlink(missing_ok=True)
        r = post("/api/courses", {"goal": "无课型兼容检查", "document_ids": [doc_id],
                                  "unit_count": 5, "depth": "standard"})
        cid_z8b = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        spy8b = " ".join(str(m.get("content") or "") for m in _read_spy()
                         if m.get("role") == "system")
        check("Z8g 不带 intent 时不注入课型块（旧客户端行为不变）",
              bool(spy8b) and "【本次课型" not in spy8b,
              f"含课型块={'【本次课型' in spy8b}")
        _cleanup_courses(cid_z8b)

        # Z8h 非法/不存在课型 id → 明确报错；**不**静默换成一个别的课型。
        # 注：原先拿预留项（recite）当反面样本，但它是**已上线**课型了 —— 反面样本必须
        # 用真正不存在的 id，否则「上线新课时型」会把这条断言弄红（2026-09-17 踩过）。
        r = post("/api/courses", {"goal": "非法课型", "document_ids": [doc_id],
                                  "intent": {"primary": "not-a-real-intent"}})
        check("Z8h 不存在的课型 id 被拒（不静默降级成别的课型）",
              r.get("code") == 1000, f"code={r.get('code')}")
        _cleanup_courses(cid_y1)

        # ── P0 回归组 ─────────────────────────────────
        # P0-2 补写 desc 的顺序校验：结构对但**顺序错** → 整体拒收（返回 0）。
        # 校验发生在写库之前，可以直接构造，无需真实课程。
        from backend.services.courses import CourseService  # noqa: E402
        import sqlite3  # noqa: E402

        _rows = [
            {"id": "L-1", "kind": "lecture", "title": "第一讲", "objective": ""},
            {"id": "L-2", "kind": "lecture", "title": "第二讲", "objective": ""},
            {"id": "L-3", "kind": "practice", "title": "练习", "objective": ""},
        ]
        _exp = [{"lessons": [{"lesson": t} for t in ("第一讲", "第二讲", "练习")]}]
        _ok_doc = {"units": [{"lessons": [
            {"title": "第一讲", "desc": {"outcomes": ["能说明「第一讲」的要点"]}},
            {"title": "第二讲", "desc": {"outcomes": ["能说明「第二讲」的要点"]}},
            {"title": "练习", "desc": {"exercise_focus": ["考察"]}},
        ]}]}
        n_ok = CourseService._apply_desc_fill(_ok_doc, _rows, _exp)
        check("P0-2a 顺序一致时按序写回（返回补写讲数）",
              n_ok == 3, f"n={n_ok}")
        _wrong_order_doc = {
            "units": [{"lessons": [
                {"title": "练习", "desc": {"exercise_focus": ["考察"]}},      # 顺序颠倒
                {"title": "第一讲", "desc": {"outcomes": []}},
                {"title": "第二讲", "desc": {"outcomes": []}},
            ]}]}
        n_bad = CourseService._apply_desc_fill(_wrong_order_doc, _rows, _exp)
        check("P0-2b 顺序不符（颠倒）→ 整体拒收返回 0", n_bad == 0, f"n={n_bad}")
        _missing_doc = {
            "units": [{"lessons": [
                {"title": "第一讲", "desc": {"outcomes": []}},
                {"title": "练习", "desc": {"exercise_focus": []}},              # 缺第二讲
            ]}]}
        n_miss = CourseService._apply_desc_fill(_missing_doc, _rows, _exp)
        check("P0-2c 数量不足（缺讲）→ 整体拒收返回 0", n_miss == 0, f"n={n_miss}")
        _empty_doc = {}
        n_empty = CourseService._apply_desc_fill(_empty_doc, _rows, _exp)
        check("P0-2d 非法/空结构 → 整体拒收返回 0", n_empty == 0, f"n={n_empty}")

        # P0-3 部分成功不算成功：mock-desc-partial 只补前 N-1 讲，
        # 端到端验证 job 必须 failed 且错误信息可见（不能静默成功）。
        set_model("mock-desc-partial")
        r = post("/api/courses", {"goal": "P0-3 部分成功回归",
                                  "document_ids": [doc_id],
                                  "unit_count": 3, "depth": "standard"})
        cid_p3 = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        # 先确认课程里有 3 讲以上正文讲（desc 需要补写的目标）。
        p3_lessons = [l.get("title") for l in get(f"/api/courses/{cid_p3}")["data"]["units"]
                      for l in l.get("lessons") or []]
        if len(p3_lessons) >= 2:
            r = post(f"/api/courses/{cid_p3}/desc:rebuild")
            j = wait_job(r["data"]["job_id"])
            msg = str(j.get("error") or "")
            check("P0-3 补写 desc 部分成功 → job 明确失败（信息可见）",
                  j["status"] == "failed" and "补上" in msg and "重试" in msg,
                  f"status={j.get('status')} error={msg}")
        else:
            check("P0-3 补写 desc 部分成功 → job 明确失败（信息可见）",
                  False, f"讲次数不足 2，无法构造部分成功场景: {len(p3_lessons)}")
        _cleanup_courses(cid_p3)

        # P0-4 并发防重入：直接往测试库插 running job 验证。
        # - 15 分钟前的 running job（僵尸）→ 不挡；
        # - 刚创建的 running job → 挡，返回 1005「任务冲突」。
        import datetime as _dt  # noqa: E402
        db4 = sqlite3.connect(str(DATA / "zhiban.db"))
        db4.row_factory = sqlite3.Row
        # 先建一门课，等它的大纲任务跑完（拿到稳定课程 id 与空闲状态）。
        r = post("/api/courses", {"goal": "P0-4 并发防重入", "document_ids": [doc_id],
                                  "intent": {"primary": "deep-read"}})
        cid_p4 = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])     # 等建课大纲完成

        # (1) 往 **同一门课** 插一条 20 分钟前的 running outline（僵尸）→ 不挡。
        zomb_id = f"job-zombie-{int(time.time())}"
        old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=20)).isoformat()
        with db4:
            db4.execute(
                "INSERT INTO course_jobs(id,course_id,lesson_id,kind,stage,status,error,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (zomb_id, cid_p4, None, "outline", "僵尸", "running", None, old_ts, old_ts))
        r = post(f"/api/courses/{cid_p4}/desc:rebuild")
        check("P0-4a 同课 20 分钟前 running 任务不挡（僵尸豁免）",
              r.get("code") == 0, f"code={r.get('code')} msg={r.get('message')}")
        # 等 desc 任务跑完（避免它与下一步的同 kind 判断互相干扰），并清掉僵尸行。
        if r.get("code") == 0:
            wait_job(r["data"]["job_id"])
        with db4:
            db4.execute("UPDATE course_jobs SET status='failed' WHERE id=?", (zomb_id,))

        # (b) 插一条「刚刚」创建的同 kind running job → 应立即被挡 1005。
        cur_id = f"new-just{int(time.time())}"
        ts_now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        with db4:
            db4.execute(
                "INSERT INTO course_jobs(id,course_id,lesson_id,kind,stage,status,error,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (cur_id, cid_p4, None, "outline", "进行中", "running", None, ts_now, ts_now))
        r4b = post(f"/api/courses/{cid_p4}/desc:rebuild")
        check("P0-4b 同课同 kind 的 running 任务 → 防重入拒绝 1005",
              r4b.get("code") == 1005, f"code={r4b.get('code')} msg={r4b.get('message')}")
        check("P0-4c 被挡后原任务保持 running（未产生新任务/未被覆盖）",
              get(f"/api/courses/jobs/{cur_id}")["data"]["status"] == "running",
              f"status={get(f'/api/courses/jobs/{cur_id}')['data'].get('status')}")
        with db4:
            db4.execute("UPDATE course_jobs SET status='failed' WHERE id=?", (cur_id,))
        db4.close()
        _cleanup_courses(cid_p4)

        # F3/F4 空 title 整批拒绝（数据灾难边界）：确认结构提交时任何讲次 title 为空
        # → 1000，绝不静默跳过（跳过会让 kept_ids 为空、课程级删除误清空整课）。
        set_model("mock-outline-5")
        r = post("/api/courses", {"goal": "F3 空标题回归", "document_ids": [doc_id],
                                  "unit_count": 3, "depth": "standard"})
        cid_f3 = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        d_f3 = get(f"/api/courses/{cid_f3}")["data"]
        before_f3 = sum(len(u.get("lessons") or []) for u in d_f3["units"])

        # F3：所有讲次 title 设为纯空格（带真实 id，验证 kept_ids 不再被清空成 0）
        bad_all = _z7_payload(d_f3, with_id=True)
        for u in bad_all:
            for l in (u.get("lessons") or []):
                l["title"] = "   "
        r_f3a = post(f"/api/courses/{cid_f3}/outline:confirm",
                     {"title": d_f3["title"], "units": bad_all})
        check("F3 所有讲次 title 为空（带 id）→ 整批拒绝 1000",
              r_f3a.get("code") == 1000, f"code={r_f3a.get('code')}")
        after_f3 = sum(len(u.get("lessons") or [])
                       for u in get(f"/api/courses/{cid_f3}")["data"]["units"])
        check("F3 空 title 拒绝后讲次数不变（未误清空整课；kept_ids 防线生效）",
              after_f3 == before_f3, f"{before_f3} → {after_f3}")

        # F4：个别讲次 title 为空（其余正常）→ 同样整批拒绝（不静默漏删该讲）
        bad_mix = _z7_payload(d_f3, with_id=True)
        if len(bad_mix) >= 2 and (bad_mix[1].get("lessons") or []):
            bad_mix[1]["lessons"][0]["title"] = ""
        r_f3b = post(f"/api/courses/{cid_f3}/outline:confirm",
                     {"title": d_f3["title"], "units": bad_mix})
        check("F4 个别讲次 title 为空 → 整批拒绝（不静默漏删该讲）",
              r_f3b.get("code") == 1000, f"code={r_f3b.get('code')}")
        _cleanup_courses(cid_f3)

        # F3-空单元 空 lessons 数组合法：用户主动删光某单元讲次 → 该单元清空、其余不受影响。
        set_model("mock-outline-5")
        r = post("/api/courses", {"goal": "F3 空单元回归", "document_ids": [doc_id],
                                  "unit_count": 3, "depth": "standard"})
        cid_f3b = r["data"]["course_id"]
        wait_job(r["data"]["job_id"])
        d_f3b = get(f"/api/courses/{cid_f3b}")["data"]
        units_f3b = _z7_payload(d_f3b, with_id=True)
        if len(units_f3b) >= 2:
            units_f3b[1]["lessons"] = []          # 清空第 2 单元讲次，保留其它单元
        r_f3c = post(f"/api/courses/{cid_f3b}/outline:confirm",
                     {"title": d_f3b["title"], "units": units_f3b})
        check("F3-空单元 单元 lessons 为空数组 → 允许（非 1000）",
              r_f3c.get("code") == 0, f"code={r_f3c.get('code')}")
        d_after = get(f"/api/courses/{cid_f3b}")["data"]
        u1 = len(d_after["units"][0].get("lessons") or [])
        u2 = len(d_after["units"][1].get("lessons") or []) if len(d_after["units"]) > 1 else -1
        u3 = len(d_after["units"][2].get("lessons") or []) if len(d_after["units"]) > 2 else 0
        check("F3-空单元 被清空单元 = 0、其余单元不受影响",
              u2 == 0 and u1 > 0 and u3 > 0, f"单元1={u1} 单元2={u2} 单元3={u3}")
        _cleanup_courses(cid_f3b)

        # ── P1. 目标留空（课型兜底）的标题与检索词 ─────────
        print("\n[P1] 目标留空 / 课型兜底句识别")
        from backend.services import courses as _cm  # noqa: E402
        from backend.services.courses import (  # noqa: E402
            _FALLBACK_GOAL_RE as _FB, _goal_hits as _GH)

        _fb_goal = "按「了解脉络型」的方式学这门课：只求大概了解：背景、人物、事件脉络、主旨"
        check("P1-3a 课型兜底句被识别（与用户自写目标区分开）",
              bool(_FB.match(_fb_goal)) and not _FB.match("学完能独立完成典型极限题"),
              f"match={bool(_FB.match(_fb_goal))}")
        check("P1-3b 兜底句不再当检索词用（直接跳过目标检索，不召回无关片段）",
              _GH(_fb_goal, []) == [], str(_GH(_fb_goal, [])[:2]))

        class _TitleProbe:                     # 只回答「材料标题」这一次查询
            def query_one(self, sql, params=()):  # noqa: ANN001, ARG002
                return {"title": "编码问题排查流程与对照"}

        _orig_get_db = _cm.get_db
        _cm.get_db = lambda: _TitleProbe()     # type: ignore[assignment]
        try:
            _t_fb = _cm.CourseService._draft_title(_fb_goal, ["doc-x"], prefer_material=True)
            _t_user = _cm.CourseService._draft_title("学完能独立完成典型题。附带说明", [], False)
        finally:
            _cm.get_db = _orig_get_db          # type: ignore[assignment]
        check("P1-3c 目标由课型兜底时临时标题取材料名（不再截出「按『…』的方…」半截串）",
              _t_fb == "《编码问题排查流程与对照》课程", f"title={_t_fb}")
        check("P1-3d 用户自写目标仍取首句（旧行为逐字保持）",
              _t_user == "学完能独立完成典型题", f"title={_t_user}")

        # ── P2. 旧版程序打开新版库：不得把 schema_version 回写降级 ──
        print("\n[P2] 迁移版本号不降级")
        from backend.db.connection import Database as _DBC  # noqa: E402
        from backend.db.migrations import (  # noqa: E402
            _get_version as _gv, _set_meta as _sm, run_migrations as _rm)

        _p = DATA / "schema-future.db"
        if _p.exists():
            _p.unlink()
        _dbx = _DBC(_p)
        try:
            _rm(_dbx)                              # 先按当前版本初始化
            _sm(_dbx, "schema_version", "99")      # 模拟「被更新版本的程序写过」
            _ver = _rm(_dbx)                       # 旧程序再启动一次
            check("P2-1a 库版本高于程序时不改写版本号（数据与版本都保持原样）",
                  _gv(_dbx) == 99 and _ver == 99, f"version={_gv(_dbx)} ret={_ver}")
        finally:
            _dbx.close_all()

        # ── M. 主题模式：没有材料时先让 AI 写出材料，再走既有链路 ──
        # 用户拍板的「两段式」：先出目录（可审阅）→ 逐章写正文 → 落成 documents →
        # 之后的建课完全走老路（引用防伪因此仍然成立）。
        print("\n[M] 主题模式（AI 先写材料）")

        def wait_gen(gid_: str, tries: int = 160) -> dict:
            for _ in range(tries):
                d = (get(f"/api/generations/{gid_}").get("data") or {})
                if d.get("status") in ("ready", "failed"):
                    return d
                time.sleep(0.5)
            return {"status": "timeout"}

        set_model("mock-normal")
        r = post("/api/materials/outline", {"topic": "Python 装饰器", "depth": "brief"})
        gid_m = (r.get("data") or {}).get("generation_id")
        check("M1a 第一步（出目录）返回 generation_id", bool(gid_m), str(r)[:140])
        g1 = wait_gen(gid_m or "")
        outline = ((g1.get("content_json") or {}).get("outline") or {})
        chs = outline.get("chapters") or []
        check("M1b 目录生成成功，且章数符合档位（brief=4 章）",
              g1.get("status") == "ready" and len(chs) == 4,
              f"status={g1.get('status')} 章数={len(chs)} err={g1.get('error')}")

        r2 = post("/api/materials/chapters", {"generation_id": gid_m})
        check("M2a 第二步接受同一任务 id", r2.get("code") == 0, str(r2)[:140])
        g2 = wait_gen(gid_m or "")
        cj2 = g2.get("content_json") or {}
        doc_ai = cj2.get("doc_id")
        md2 = g2.get("content_md") or ""
        n_head = md2.count("\n## ")
        check("M2b 材料落库并回传 doc_id", bool(doc_ai), str(cj2)[:160])
        check("M2c 进度口径：content_md 里 `## ` 条数 == 章数（前端据此显示 N/M 章）",
              n_head == len(chs), f"##={n_head} 章数={len(chs)}")
        det_doc = {}
        if doc_ai:
            det_doc = (get(f"/api/documents/{doc_ai}").get("data") or {}).get("document") or {}
        check("M2d 材料标记为 AI 生成（source_type=ai + 「AI 生成」标签）",
              str(det_doc.get("source_type")) == "ai"
              and "AI 生成" in (det_doc.get("tags") or []),
              f"source_type={det_doc.get('source_type')} tags={det_doc.get('tags')}")

        set_model("mock-spy-lecture")
        r3 = post("/api/courses", {"goal": "学会装饰器", "document_ids": [doc_ai],
                                   "unit_count": 2})
        cid_m = r3["data"]["course_id"]
        wait_job(r3["data"]["job_id"])
        d_m = get(f"/api/courses/{cid_m}")["data"]
        les_m = [l for u in (d_m.get("units") or []) for l in u["lessons"]
                 if l.get("kind") == "lecture"]
        r4 = post(f"/api/courses/lessons/{les_m[0]['id']}/lecture") if les_m else {"data": {}}
        if les_m:
            wait_job(r4["data"]["job_id"])
        det_m = get(f"/api/courses/lessons/{les_m[0]['id']}")["data"] if les_m else {}
        check("M3 用 AI 生成的材料能正常建课（大纲 + 讲义都出得来，说明材料真的可检索）",
              bool(les_m) and bool(det_m.get("slides")),
              f"讲次数={len(les_m)} 页数={len(det_m.get('slides') or [])}")

        set_model("mock-material-short")   # 单章正文故意太短 → 重试后仍失败 → 留占位
        r5 = post("/api/materials/outline", {"topic": "失败口径测试", "depth": "brief"})
        gid_f = (r5.get("data") or {}).get("generation_id")
        wait_gen(gid_f or "")
        post("/api/materials/chapters", {"generation_id": gid_f})
        g6 = wait_gen(gid_f or "")
        md6 = g6.get("content_md") or ""
        check("M4 单章失败：任务仍算成功、材料里留占位（不整体失败、不浪费已生成部分）",
              g6.get("status") == "ready" and "本章生成失败" in md6,
              f"status={g6.get('status')} 含占位={'本章生成失败' in md6}")

        set_model("mock-normal")   # 桩是确定性的 → 同主题重跑内容完全一致 → 应命中去重
        r7 = post("/api/materials/outline", {"topic": "Python 装饰器", "depth": "brief"})
        gid_d = (r7.get("data") or {}).get("generation_id")
        wait_gen(gid_d or "")
        post("/api/materials/chapters", {"generation_id": gid_d})
        g8 = wait_gen(gid_d or "")
        cj8 = g8.get("content_json") or {}
        check("M5 相同内容重复生成 → 命中已有材料并给出提示（不产生重复文档）",
              bool(cj8.get("skip_reason")) and cj8.get("doc_id") == doc_ai,
              str(cj8)[:170])
        check("M6 空主题被拒（不静默生成一份空材料）",
              post("/api/materials/outline", {"topic": "   "}).get("code") == 1000,
              str(post("/api/materials/outline", {"topic": "   "}))[:120])
        _cleanup_courses(cid_m)

        # ── I. 删除 ────────────────────────────────────
        print("\n[I] 删除课程")
        with httpx.Client(trust_env=False) as cli:
            r = cli.delete(f"{BACKEND}/api/courses/{cid2}", timeout=20).json()
        check("I1 删除成功", r["code"] == 0 and r["data"]["deleted"] is True, str(r))
        r = get(f"/api/courses/{cid2}")
        check("I2 课程已不存在", r["code"] == 1001, str(r.get("code")))

    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
