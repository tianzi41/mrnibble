"""大纲单元数修复自测（无网络、无真实 Key，可重复运行）。

对应 bug：编辑课程结构时填写「一共生成五章…」点「重新生成」，结果仍是 3 章。

覆盖：
  1) _unit_count_from_note 纯函数（中文/阿拉伯数字、单位词、序数排除、clamp、取最后一个）
  2) _validate_outline 不再误截（unit_count>=1 按 N 截、0 按 8 截，证明防御还在）
  3) regenerate_outline 把 unit_count 正确传进 _run_outline（三种入口）
  4) 端到端：mock-outline-units 从提示词解析「单元数固定为 N 个」返回 N 个单元

用法::

    .venv/Scripts/python.exe dev/outline_units_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from backend.services.courses import _unit_count_from_note, CourseService
from backend.utils.timeutil import now_iso

PY = ROOT / ".venv" / "Scripts" / "python.exe"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    flag = "✅" if cond else "❌"
    print(f"  {flag} {name}" + (f"  | {detail}" if (detail and not cond) else ""))


# ───────────────────────────────────────────────────────────
# 组 1：_unit_count_from_note 纯函数
# ───────────────────────────────────────────────────────────
def group1() -> None:
    print("\n[1] _unit_count_from_note 解析")

    cases = [
        # (note, 期望, 说明)
        ("一共生成五章，最后一个单元要求是背诵的单元", 5,
         "中文『五章』命中；『最后一个单元』是序数(带『后』)不算数量"),
        ("改成 3 个单元", 3, "阿拉伯数字 +『个单元』"),
        ("不要 3 章，要 5 章", 5, "取最后一个匹配（用户先写旧值再写新值）"),
        ("五个部分", 5, "中文数字 +『个部分』"),
        ("多加一些例题，保留易错点", None, "无『数字+单位词』，不误判『一些』"),
        ("12 章", 12, "阿拉伯 12，在 1~12 域内"),
        ("五", None, "中文数字但无单位词，不算"),
        ("第十单元", None, "序数（带『第』）前缀排除，不是『10 个单元』"),
    ]
    for note, want, why in cases:
        got = _unit_count_from_note(note)
        check(f"note={note!r} → {want}", got == want,
              f"得到 {got}（{why}）")

    # 越界输入：clamp 到 12（与单元数域 1~12 一致）。
    got20 = _unit_count_from_note("20 章")
    check("note='20 章' → 12（clamp 到上限，而非丢成 None）", got20 == 12,
          f"得到 {got20}")
    print("     · 越界选择理由：单元数域为 1~12（router 上限、service clamp 0~12），"
          "把 20 硬 clamp 到 12 比静默回落到 None 更贴近用户意图，"
          "也避免『又变回 3 章』这类 bug 复发。")


# ───────────────────────────────────────────────────────────
# 组 2：_validate_outline 不再误截
# ───────────────────────────────────────────────────────────
def _make_obj(n_units: int) -> dict:
    """构造 n_units 个单元、每单元 2 讲且 objective 合规的大纲对象。"""
    units = []
    for i in range(1, n_units + 1):
        units.append({
            "title": f"单元{i}",
            "summary": f"第{i}单元简介",
            "lessons": [
                {"title": f"单元{i}·讲1", "objective": "能运用核心概念解题",
                 "kind": "lecture", "depth": "define"},
                {"title": f"单元{i}·讲2", "objective": "能完成典型例题",
                 "kind": "lecture", "depth": "derive"},
            ],
        })
    return {"title": "测试课程", "summary": "x", "units": units}


def group2() -> None:
    print("\n[2] _validate_outline 不再误截")
    obj5 = _make_obj(5)

    v5 = CourseService._validate_outline(obj5, 5)
    check("_validate_outline(obj, 5) 返回 5 个单元", v5 is not None and len(v5["units"]) == 5,
          f"得到 {None if v5 is None else len(v5['units'])}")

    v3 = CourseService._validate_outline(obj5, 3)
    check("_validate_outline(obj, 3) 返回 3 个单元（既有防御仍在）",
          v3 is not None and len(v3["units"]) == 3,
          f"得到 {None if v3 is None else len(v3['units'])}")

    v0 = CourseService._validate_outline(obj5, 0)
    check("_validate_outline(obj, 0) 自动模式保留全部（≤8），此处 5",
          v0 is not None and len(v0["units"]) == 5,
          f"得到 {None if v0 is None else len(v0['units'])}")


# ───────────────────────────────────────────────────────────
# 组 3：regenerate_outline 把 unit_count 正确传进 _run_outline
# ───────────────────────────────────────────────────────────
def group3() -> None:
    print("\n[3] regenerate_outline → _run_outline 的 unit_count 传递（monkeypatch）")

    # 进程内临时库，避免起服务。
    tmp = Path(tempfile.mkdtemp(prefix="zhiban-units-"))
    os.environ["ZHIBAN_DATA_DIR"] = str(tmp)
    from backend.db.connection import get_db
    db = get_db()
    db.migrate()  # 建表（Database 构造不自动迁移）
    cid = "course_units_test"
    db.execute(
        "INSERT INTO courses(id,title,goal,level,depth,unit_count,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (cid, "测试课", "学大纲", "beginner", "standard", 3, now_iso(), now_iso()),
    )

    captured: list[dict] = []

    def fake_run(self, c, job_id, document_ids, goal, level, depth, unit_count, note=""):
        captured.append({"unit_count": unit_count, "note": note})
        self._finish_job(job_id)  # 否则『上一个任务还在进行』会挡住后续调用

    orig = CourseService._run_outline
    CourseService._run_outline = fake_run  # type: ignore[assignment]
    svc = CourseService.get_instance()
    try:
        # 入口 a：payload 显式给 5 → 5
        captured.clear()
        svc.regenerate_outline(cid, {"unit_count": 5})
        time.sleep(0.4)
        check("payload 显式 unit_count=5 → 传给 _run_outline 的是 5",
              captured and captured[-1]["unit_count"] == 5,
              f"captured={captured}")

        # 入口 b：payload 不给，但 note 含『五章』 → 5（用户实际走的路径）
        captured.clear()
        svc.regenerate_outline(cid, {"note": "一共生成五章，最后一个单元要求是背诵的单元"})
        time.sleep(0.4)
        check("payload 不给 + note『五章』 → 5",
              captured and captured[-1]["unit_count"] == 5,
              f"captured={captured}")

        # 入口 c：payload 显式给 0（自动）→ 0
        captured.clear()
        svc.regenerate_outline(cid, {"unit_count": 0})
        time.sleep(0.4)
        check("payload 显式 unit_count=0 → 0（自动，不被 or 吃掉）",
              captured and captured[-1]["unit_count"] == 0,
              f"captured={captured}")

        # 入口 d：payload 不给，note『3 个单元』 → 3（note 优先于旧值 3，证明解析生效）
        captured.clear()
        svc.regenerate_outline(cid, {"note": "改成 3 个单元"})
        time.sleep(0.4)
        check("payload 不给 + note『3 个单元』 → 3",
              captured and captured[-1]["unit_count"] == 3,
              f"captured={captured}")
    finally:
        CourseService._run_outline = orig  # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────────────────────────────────────────
# 组 4：端到端（真实 mock_llm + 后端，mock-outline-units 解析提示词）
# ───────────────────────────────────────────────────────────
def _wait_http(url: str, timeout: float = 40.0) -> bool:
    import httpx
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with httpx.Client(trust_env=False) as cli:
                if cli.get(url, timeout=3).status_code < 500:
                    return True
        except Exception:
            time.sleep(0.6)
    return False


def _insert_fixture(db_path: Path, cid: str, doc_id: str) -> None:
    """直接往后端库插一份『已解析』假材料 + 课程，让 material_overview 有 hits。"""
    conn = sqlite3.connect(str(db_path))
    try:
        now = now_iso()
        conn.execute(
            "INSERT INTO documents(id,title,source_type,fmt,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (doc_id, "假材料", "file", "txt", "ready", now, now),
        )
        for i in range(1, 4):
            conn.execute(
                "INSERT INTO chunks(id,document_id,ordinal,text,text_seg,page_no,section,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (f"{doc_id}_ck{i}", doc_id, i, f"材料第{i}段内容", f"材料第{i}段内容", i,
                 f"第{i}章", now),
            )
        conn.execute(
            "INSERT INTO courses(id,title,goal,level,depth,unit_count,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (cid, "E2E课", "学大纲", "beginner", "standard", 3, now, now),
        )
        conn.execute(
            "INSERT INTO course_documents(course_id,document_id) VALUES(?,?)",
            (cid, doc_id),
        )
        conn.commit()
    finally:
        conn.close()


def group4() -> None:
    print("\n[4] 端到端：mock-outline-units 解析提示词返回 N 个单元")
    import httpx

    tmp = Path(tempfile.mkdtemp(prefix="zhiban-units-e2e-"))
    backend_url = "http://127.0.0.1:8768"
    mock_url = "http://127.0.0.1:8769"
    env = {**os.environ, "ZHIBAN_DATA_DIR": str(tmp), "ZHIBAN_PORT": "8768",
           "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    procs = [
        subprocess.Popen([str(PY), str(ROOT / "dev" / "mock_llm.py")],
                         env={**env, "ZHIBAN_MOCK_PORT": "8769"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        if not _wait_http(f"{mock_url}/health"):
            check("端到端：mock LLM 启动", False, "mock 未启动")
            return
        if not _wait_http(f"{backend_url}/api/health"):
            check("端到端：后端启动", False, "后端未启动")
            return
        check("端到端：服务启动", True)

        db_path = tmp / "zhiban.db"
        cid = "course_e2e"
        doc_id = "doc_e2e"
        _insert_fixture(db_path, cid, doc_id)
        # 另一个独立课程（同样旧值为 3），用于验证「note 不提数量 → 沿用旧值 3，
        # 模型不会擅自把 3 扩成 5」这一防御（第一个课程会被第一次 regen 改成 5）。
        _insert_fixture(db_path, "course_e2e_c", "doc_e2e_c")

        # 指向 mock，模型用 mock-outline-units（从提示词解析『单元数固定为 N 个』）。
        with httpx.Client(trust_env=False) as cli:
            r = cli.put(f"{backend_url}/api/settings", json={
                "llm": {"base_url": f"{mock_url}/v1",
                        "model": "mock-outline-units",
                        "api_key": "sk-test-units"}})
            assert r.json()["code"] == 0, r.text

            def regen(note: str, payload: dict | None = None):
                body = payload or {}
                if note is not None:
                    body["note"] = note
                resp = cli.post(f"{backend_url}/api/courses/{cid}/outline:regenerate",
                                json=body, timeout=30).json()
                assert resp.get("code") == 0, resp
                job_id = resp["data"]["job_id"]
                # 等任务
                for _ in range(120):
                    job = cli.get(f"{backend_url}/api/courses/jobs/{job_id}").json()["data"]
                    if job["status"] in ("ready", "failed"):
                        break
                    time.sleep(0.5)
                course = cli.get(f"{backend_url}/api/courses/{cid}").json()["data"]
                units = course.get("units") or []
                return len(units), course.get("status")

            # 关键：note 写『五章』应真的出 5 个单元（修复点）
            n5, st5 = regen("一共生成五章，最后一个单元要求是背诵的单元")
            check("端到端：note『五章』→ 落库 5 个单元", n5 == 5, f"得到 {n5}（status={st5}）")

            # 对照：note 不含数量、不显式给 unit_count → 沿用旧值 3 → 3 个单元
            # （用独立课程 course_e2e_c，其旧值仍是 3，不会被第一次 regen 改成 5）
            def regen_on(course_id, note, payload=None):
                body = payload or {}
                if note is not None:
                    body["note"] = note
                r2 = cli.post(f"{backend_url}/api/courses/{course_id}/outline:regenerate",
                              json=body, timeout=30).json()
                assert r2.get("code") == 0, r2
                jid = r2["data"]["job_id"]
                for _ in range(120):
                    jb = cli.get(f"{backend_url}/api/courses/jobs/{jid}").json()["data"]
                    if jb["status"] in ("ready", "failed"):
                        break
                    time.sleep(0.5)
                return len(cli.get(f"{backend_url}/api/courses/{course_id}").json()["data"].get("units") or [])

            n3 = regen_on("course_e2e_c", "多加一些例题，保留易错点")
            check("端到端：note 无数量 → 沿用旧值 3 个单元（防御未破坏）",
                  n3 == 3, f"得到 {n3}")

            # 显式 unit_count=7 → 7 个单元
            n7, st7 = regen(None, {"unit_count": 7})
            check("端到端：显式 unit_count=7 → 7 个单元", n7 == 7,
                  f"得到 {n7}（status={st7}）")
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    group1()
    group2()
    group3()
    group4()
    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
