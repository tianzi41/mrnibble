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
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)

    env = {**os.environ, "ZHIBAN_DATA_DIR": str(DATA), "ZHIBAN_PORT": "8762",
           "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    procs = [
        # mock 端口用 ZHIBAN_MOCK_PORT 固定为 8763，与主自测（8761）互不干扰。
        subprocess.Popen([str(PY), str(ROOT / "dev" / "mock_llm.py")],
                         env={**env, "ZHIBAN_MOCK_PORT": "8763"},
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
