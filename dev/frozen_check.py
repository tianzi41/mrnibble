"""冻结态（打包后 exe）端到端验证：一次跑完，避免进程被中途回收。

用法：
    # 用默认目录 dist4/知伴
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe dev/frozen_check.py
    # 验证其它构建目录
    ZHIBAN_DIST=dist5 .venv/Scripts/python.exe dev/frozen_check.py
    # 用独立数据目录验证（不污染绿色包里已迁移的真实数据）
    ZHIBAN_DIST=dist5 ZHIBAN_DATA_DIR=.tmp/frozen-data .venv/Scripts/python.exe dev/frozen_check.py

注意：本脚本会**写入**数据目录（上传测试文件、改模型配置、建会话）。若绿色包里
已经带了用户的真实数据，务必用 ``ZHIBAN_DATA_DIR`` 指向一个临时目录再跑。
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

ROOT = Path(__file__).resolve().parents[1]
# 构建目录可用环境变量覆盖，便于重新打包后验证新产物。
DIST = Path(os.environ.get("ZHIBAN_DIST", "dist4"))
EXE = (DIST if DIST.is_absolute() else ROOT / DIST) / "知伴" / "知伴.exe"
# 数据目录：与后端一致的优先级（环境变量覆盖 > exe 同级 data）。
# 冻结版 exe 继承本进程环境变量，因此这里设置即可生效。
DATA_DIR = Path(os.environ.get("ZHIBAN_DATA_DIR") or (EXE.parent / "data"))
if not DATA_DIR.is_absolute():
    DATA_DIR = (ROOT / DATA_DIR).resolve()
BACKEND = "http://127.0.0.1:8760"
MOCK = "http://127.0.0.1:8761"
FAKE_KEY = "sk-frozen-test-1234567890"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  | {detail}" if (detail and not cond) else ""))


def wait(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=3, trust_env=False).status_code < 500:
                return True
        except Exception:
            time.sleep(0.6)
    return False


def main():
    # 独立数据目录，避免与开发态混淆
    proc = None
    if not wait(f"{BACKEND}/api/health", 3):
        print("启动冻结版 exe ...")
        proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mock = subprocess.Popen([sys.executable, str(ROOT / "dev" / "mock_llm.py")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait(f"{BACKEND}/api/health"), "冻结版服务未启动"
        assert wait(f"{MOCK}/health"), "mock 未启动"
        C = dict(trust_env=False, timeout=90)  # 本机直连，不走系统代理

        print("[1] 路由完整性")
        r = httpx.get(f"{BACKEND}/api/system/info", **C).json()
        check("1.1 system/info", r["code"] == 0)
        for p in ("/api/documents", "/api/conversations", "/api/memories",
                  "/api/generations", "/api/asr/status", "/api/tts/status", "/api/settings"):
            code = httpx.get(f"{BACKEND}{p}", **C).status_code
            check(f"1.2 {p}", code == 200, str(code))
        # /settings/models 需要先填 base_url 才能工作（未配置时按设计返回 400 +
        # 错误码 2000）。这里只验证**路由存在**（非 404）；填好端点后的真实拉取
        # 在 [3b.1] 断言，那一步才是有意义的验证。
        r = httpx.get(f"{BACKEND}/api/settings/models?target=llm", **C)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check("1.2 /api/settings/models 路由存在",
              r.status_code != 404 and (r.status_code == 200 or body.get("code") == 2000),
              f"{r.status_code} {body.get('code')}")

        print("[2] 密钥配置")
        httpx.put(f"{BACKEND}/api/settings", json={
            "llm": {"base_url": f"{MOCK}/v1", "model": "mock-normal", "api_key": FAKE_KEY}}, **C)
        pub = httpx.get(f"{BACKEND}/api/settings", **C).json()["data"]
        check("2.1 Key 只回掩码", FAKE_KEY not in json.dumps(pub, ensure_ascii=False))

        print("[3] 上传 → 检索 → 问答")
        md = io.BytesIO("## 3.2 洛必达法则\n\n洛必达法则是求未定式极限的重要方法，"
                        "适用于 0/0 型或 ∞/∞ 型未定式。\n".encode("utf-8"))
        up = httpx.post(f"{BACKEND}/api/documents/upload",
                        files={"files": ("frozen.md", md, "text/markdown")}, **C).json()
        did = up["data"]["documents"][0]["id"]
        for _ in range(40):
            d = httpx.get(f"{BACKEND}/api/documents/{did}", **C).json()["data"]["document"]
            if d["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        check("3.1 解析完成", d["status"] == "ready", str(d.get("error")))
        hits = httpx.post(f"{BACKEND}/api/retrieve", json={"query": "洛必达法则"}, **C).json()["data"]["hits"]
        check("3.2 检索命中", len(hits) > 0)
        cid = httpx.post(f"{BACKEND}/api/conversations", json={}, **C).json()["data"]["id"]
        ans = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid, "message": "洛必达法则怎么用？"}, **C).json()["data"]
        check("3.3 回答含引用且无原始角标", "[[c:" not in ans["answer"] and len(ans["citations"]) > 0)

        print("[3b] 模型配置完整性与重新解析")
        r = httpx.get(f"{BACKEND}/api/settings/models?target=llm", **C).json()
        check("3b.1 可拉取端点模型列表", "mock-normal" in (r["data"]["models"] or []),
              str(r["data"]["models"])[:60])
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": ""}}, **C)
        t = httpx.post(f"{BACKEND}/api/settings/test", json={"target": "llm"}, **C).json()
        check("3b.2 缺模型名被明确拒绝",
              t["code"] == 2000 and "模型名" in t["message"], f"{t['code']} {t['message']}")
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-normal"}}, **C)
        t = httpx.post(f"{BACKEND}/api/settings/test", json={"target": "llm"}, **C).json()
        check("3b.3 完整配置无告警",
              t["code"] == 0 and not (t["data"] or {}).get("warning"), str(t.get("data"))[:60])
        rp = httpx.post(f"{BACKEND}/api/documents/{did}/reparse", **C).json()
        check("3b.4 重新解析可用且仍为 ready", rp["code"] == 0 and
              (((rp.get("data") or {}).get("document") or {}).get("status") == "ready"),
              str(rp.get("message")))

        print("[3c] 材料概览兜底（上传后提问不应答「没收到文件」）")
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-echo-context"}}, **C)

        def ask_scoped(doc_ids, message):
            c = httpx.post(f"{BACKEND}/api/conversations",
                           json={"mode": "normal", "document_ids": doc_ids}, **C).json()["data"]
            return httpx.post(f"{BACKEND}/api/chat", json={
                "conversation_id": c["id"], "message": message}, **C).json()["data"]

        a = ask_scoped([did], "看下 frozen.md 的具体内容，然后安排学习")
        check("3c.1 文件名问法 → 材料进提示词",
              "【材料概览】" in a["answer"] and "[材料1]" in a["answer"], a["answer"][:80])
        a = ask_scoped([did], "帮我总结一下这篇文档")
        check("3c.2 文档级问法 → 材料进提示词",
              "【材料概览】" in a["answer"] and "[材料1]" in a["answer"], a["answer"][:80])
        a = ask_scoped([did], "量子纠缠的三体拓扑结构是什么？")
        check("3c.3 材料外问题仍回「材料中未提及」且零引用",
              "材料中未提及" in a["answer"] and not a.get("citations"), a["answer"][:80])
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-normal"}}, **C)

        print("[4] 引导式护栏（违规模型）")
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-violate-first-turn"}}, **C)
        cid2 = httpx.post(f"{BACKEND}/api/conversations", json={"mode": "guided"}, **C).json()["data"]["id"]
        g = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid2, "message": "洛必达法则怎么用？直接告诉我答案", "guided": True}, **C).json()["data"]
        gui = g.get("guided") or {}
        check("4.1 首轮不给最终答案", gui.get("final_answer", "") == "")
        check("4.2 拆解 ≥2 且追问 ≥1", len(gui.get("decomposition_steps", [])) >= 2
              and len(gui.get("follow_up_questions", [])) >= 1)

        print("[4b] 引导式连续推进（不能原地复读同一问题）")
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-stall-guided"}}, **C)
        c3 = httpx.post(f"{BACKEND}/api/conversations",
                        json={"mode": "guided", "document_ids": [did]}, **C).json()["data"]
        a1 = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": c3["id"], "message": "开始学习", "guided": True}, **C).json()["data"]
        a2 = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": c3["id"], "message": "中文路径本身没问题吧",
            "guided": True}, **C).json()["data"]
        q1 = (a1.get("guided") or {}).get("follow_up_questions") or []
        q2 = (a2.get("guided") or {}).get("follow_up_questions") or []
        check("4b.1 第二轮追问与第一轮不同（不复读）",
              bool(q1) and bool(q2) and q1 != q2, f"{q1[:1]} vs {q2[:1]}")
        check("4b.2 教学线程未被打断（第二轮仍有引导载荷）",
              bool(a2.get("guided")), str(sorted(a2.keys()))[:80])
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-normal"}}, **C)

        print("[5] 生成与导出")
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-flashcard"}}, **C)
        gid = httpx.post(f"{BACKEND}/api/generations", json={
            "type": "flashcard", "document_ids": [did], "params": {"count": 3}}, **C).json()["data"]["generation_id"]
        for _ in range(60):
            gen = httpx.get(f"{BACKEND}/api/generations/{gid}", **C).json()["data"]
            if gen["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        check("5.1 闪卡生成", gen["status"] == "ready", str(gen.get("error")))
        ex = httpx.post(f"{BACKEND}/api/exports", json={
            "generation_id": gid, "format": "anki"}, **C).json()
        check("5.2 Anki 导出", ex["code"] == 0 and ex["data"]["file_name"].endswith(".apkg"),
              ex.get("message", ""))

        print("[6] 本地语音识别（冻结态，模型随包）")
        st = httpx.get(f"{BACKEND}/api/asr/status", **C).json()["data"]
        check("6.1 模型可用", st["available"] is True, json.dumps(st))
        wav = (ROOT / "dev" / "fixtures" / "zh_note.wav").read_bytes()
        t = httpx.post(f"{BACKEND}/api/asr/transcribe",
                       files={"audio": ("a.wav", wav, "audio/wav")}, **C).json()
        check("6.2 中文转写", "思维导图" in t["data"]["text"], t["data"]["text"] if t["code"] == 0 else str(t))

        print("[7] 课程链路（冻结态：大纲 → 讲义 → 练习 → 判分 → 单元总结）")
        httpx.put(f"{BACKEND}/api/settings", json={
            "llm": {"base_url": f"{MOCK}/v1", "model": "mock-outline", "api_key": FAKE_KEY}}, **C)
        r = httpx.post(f"{BACKEND}/api/courses", json={
            "goal": "学会洛必达法则并判断适用条件", "document_ids": [did],
            "unit_count": 2}, **C).json()
        check("7.1 创建课程", r["code"] == 0, str(r.get("message")))
        cid, jid = r["data"]["course_id"], r["data"]["job_id"]
        j = {}
        for _ in range(160):
            j = httpx.get(f"{BACKEND}/api/courses/jobs/{jid}", **C).json()["data"]
            if j["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        check("7.2 大纲生成完成", j.get("status") == "ready", str(j.get("error")))
        course = httpx.get(f"{BACKEND}/api/courses/{cid}", **C).json()["data"]
        check("7.3 课程就绪且含单元",
              course["status"] == "ready" and len(course["units"]) == 2,
              f'{course["status"]} units={len(course["units"])}')
        lessons = [l for u in course["units"] for l in u["lessons"]]
        check("7.4 讲次已落库", len(lessons) == 6, str(len(lessons)))
        lid = lessons[0]["id"]

        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-lecture"}}, **C)
        r = httpx.post(f"{BACKEND}/api/courses/lessons/{lid}/lecture", **C).json()
        for _ in range(160):
            j = httpx.get(f"{BACKEND}/api/courses/jobs/{r['data']['job_id']}", **C).json()["data"]
            if j["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        check("7.5 讲义生成完成", j.get("status") == "ready", str(j.get("error")))
        les = httpx.get(f"{BACKEND}/api/courses/lessons/{lid}", **C).json()["data"]
        check("7.6 白板含卡片与关键术语",
              len((les.get("board") or {}).get("cards") or []) >= 3
              and len((les.get("board") or {}).get("keypoints") or []) >= 2,
              json.dumps(les.get("board"))[:80])
        check("7.7 讲义引用已回填页码来源",
              all(c.get("document_title") for c in (les.get("citations") or [])),
              json.dumps(les.get("citations"))[:80])
        # 课件 / 讲稿分离：冻结版也必须带出新结构
        check("7.7a 冻结版带出独立课件与讲稿",
              len(les.get("slides") or []) >= 3 and len(les.get("scripts") or []) >= 3,
              f"slides={len(les.get('slides') or [])} scripts={len(les.get('scripts') or [])}")
        check("7.7b 课件讲稿逐页对应",
              len(les.get("slides") or []) == len(les.get("scripts") or [])
              and {s["id"] for s in les.get("slides") or []}
                  == {sc["slide_id"] for sc in les.get("scripts") or []})

        # 讲稿去雷同护栏：mock 故意把讲稿写成照念课件，冻结版也必须拦住
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-lecture-mirror"}}, **C)
        r = httpx.post(f"{BACKEND}/api/courses/lessons/{lid}/lecture", **C).json()
        for _ in range(160):
            j = httpx.get(f"{BACKEND}/api/courses/jobs/{r['data']['job_id']}", **C).json()["data"]
            if j["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        _les2 = httpx.get(f"{BACKEND}/api/courses/lessons/{lid}", **C).json()["data"]
        _scripts2 = _les2.get("scripts") or []
        check("7.7c 冻结版拦住「讲稿照念课件」",
              bool(_scripts2) and all(sc.get("cue") in ("rewritten", "expanded") for sc in _scripts2),
              str([sc.get("cue") for sc in _scripts2]))

        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-practice"}}, **C)
        r = httpx.post(f"{BACKEND}/api/courses/lessons/{lid}/practice",
                       json={"count": 5}, **C).json()
        for _ in range(160):
            j = httpx.get(f"{BACKEND}/api/courses/jobs/{r['data']['job_id']}", **C).json()["data"]
            if j["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        check("7.8 出题完成", j.get("status") == "ready", str(j.get("error")))
        qs = httpx.get(f"{BACKEND}/api/courses/lessons/{lid}/practice", **C).json()["data"]["items"]
        check("7.9 题目已生成且不含答案",
              len(qs) == 5 and all("answer" not in q for q in qs), str(len(qs)))
        by_type = {q["type"]: q for q in qs}
        # 单题即时判定（逐题反馈用）：答完立刻知道对错与解析，且不写库
        _q1 = next(q for q in qs if q["type"] == "single")
        _rc = httpx.post(f"{BACKEND}/api/courses/lessons/{lid}/check",
                         json={"question_id": _q1["id"], "answer": 0}, **C).json()
        _rd = _rc.get("data") or {}
        check("7.9b 冻结版单题即时判定可用",
              _rc["code"] == 0 and "expected_index" in _rd
              and _rd.get("correct") == (0 == _rd.get("expected_index"))
              and bool((_rd.get("explanation") or "").strip()),
              str(_rd)[:140])
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-grade"}}, **C)
        r = httpx.post(f"{BACKEND}/api/courses/lessons/{lid}/grade", json={"answers": [
            {"question_id": by_type["single"]["id"], "answer": 0},
            {"question_id": by_type["boolean"]["id"], "answer": 1},
            {"question_id": by_type["fill_in"]["id"], "answer": "0/0 型 与 ∞/∞ 型"},
        ]}, **C).json()
        res = r["data"] if r["code"] == 0 else {}
        check("7.10 判分可用", r["code"] == 0 and res.get("total", 0) >= 1, str(r.get("message")))
        check("7.11 客观题判分结果确定",
              any(x["correct"] for x in res.get("results", []))
              and any(not x["correct"] for x in res.get("results", [])),
              json.dumps(res.get("results"))[:120])

        # 单元总结：先把该单元讲次全部标记完成
        for l in course["units"][0]["lessons"]:
            httpx.put(f"{BACKEND}/api/courses/lessons/{l['id']}",
                      json={"complete": True}, **C)
        httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": "mock-summary"}}, **C)
        uid = course["units"][0]["id"]
        r = httpx.post(f"{BACKEND}/api/courses/units/{uid}/summary", **C).json()
        for _ in range(160):
            j = httpx.get(f"{BACKEND}/api/courses/jobs/{r['data']['job_id']}", **C).json()["data"]
            if j["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        s = httpx.get(f"{BACKEND}/api/courses/units/{uid}/summary", **C).json()["data"]
        check("7.12 单元总结生成", s["status"] == "ready", str(s.get("error")))
        check("7.13 总结含薄弱点与统计",
              bool((s.get("summary") or {}).get("weak_points"))
              and bool((s.get("summary") or {}).get("stats")),
              json.dumps(s.get("summary"))[:100])

        # 导出（讲义 Markdown）与课程进度
        ex = httpx.get(f"{BACKEND}/api/courses/lessons/{lid}/export?kind=lesson", **C)
        check("7.14 讲义可导出 Markdown",
              ex.status_code == 200 and ex.text.lstrip().startswith("#"), str(ex.status_code))
        course = httpx.get(f"{BACKEND}/api/courses/{cid}", **C).json()["data"]
        check("7.15 进度随完成情况更新",
              course["progress"]["done_lessons"] >= 1
              and course["progress"]["percent"] > 0,
              json.dumps(course["progress"]))

        print("[8] 日志脱敏")
        hit = 0
        for f in (DATA_DIR / "logs").glob("*.log*"):
            hit += f.read_text(encoding="utf-8", errors="ignore").count(FAKE_KEY)
        check("8.1 日志无 Key 明文", hit == 0, f"命中{hit}")

    finally:
        for p in (mock,):
            try:
                p.terminate(); p.wait(timeout=5)
            except Exception:
                p.kill()
        if proc is not None:
            try:
                proc.terminate(); proc.wait(timeout=8)
            except Exception:
                proc.kill()

    print("\n" + "=" * 60)
    print(f"冻结态验证：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败：", "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
