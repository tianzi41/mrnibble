"""T06~T11 集成自测（无网络、无真实 Key、可重复）。

启动 mock LLM(8761) + 后端(8760，独立数据目录)，逐项验证架构文档 §13 的判据
与四条结构性红线。用法::

    .venv/Scripts/python.exe dev/t06_t11_check.py
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".tmp" / ("test-data-t06-%d" % int(time.time()))
BACKEND = "http://127.0.0.1:8760"
MOCK = "http://127.0.0.1:8761"
FAKE_KEY = "sk-test-1234567890abcdef"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{('  ← ' + detail) if (detail and not cond) else ''}")
    print(f"  {'✅' if cond else '❌'} {name}{('  | ' + detail) if (detail and not cond) else ''}")


def wait_http(url: str, timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=3).status_code < 500:
                return True
        except Exception:
            time.sleep(0.6)
    return False


def put_model(model: str) -> None:
    r = httpx.put(f"{BACKEND}/api/settings", json={"llm": {"model": model}}, timeout=10)
    assert r.json()["code"] == 0, r.text


def main() -> int:
    import shutil

    DATA.mkdir(parents=True)

    env = {**os.environ, "MRNIBBLE_DATA_DIR": str(DATA), "PYTHONPATH": str(ROOT / "src"),
           "PYTHONIOENCODING": "utf-8"}
    mock = subprocess.Popen([str(PY), str(ROOT / "dev" / "mock_llm.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    be = subprocess.Popen([str(PY), "-m", "backend.main"], cwd=str(ROOT), env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_http(f"{MOCK}/health"), "mock LLM 未启动"
        assert wait_http(f"{BACKEND}/api/health"), "后端未启动"
        print("两个服务已启动\n")

        # ── A. 设置与密钥 ───────────────────────────────
        print("[A] 设置 / 密钥安全")
        r = httpx.put(f"{BACKEND}/api/settings", json={
            "llm": {"base_url": f"{MOCK}/v1", "model": "mock-normal", "api_key": FAKE_KEY}
        }, timeout=10).json()
        check("A1 保存设置成功", r["code"] == 0, r.get("message", ""))
        pub = httpx.get(f"{BACKEND}/api/settings", timeout=10).json()["data"]
        check("A2 Key 仅回掩码", pub["llm"]["api_key_masked"] == "sk-****cdef", str(pub["llm"]))
        check("A3 响应不含明文 Key", FAKE_KEY not in json.dumps(pub, ensure_ascii=False))

        # ── B. 上传与解析 ───────────────────────────────
        print("[B] 文档上传 / 解析")
        md = io.BytesIO((
            "# 第3章 导数与极限\n\n"
            "## 3.1 极限的定义\n\n"
            "极限描述的是函数在某点附近的变化趋势。当自变量 x 趋于某一点时，"
            "若函数值无限接近某个确定的数，就称该数为函数在此点的极限。\n\n"
            "## 3.2 洛必达法则\n\n"
            "洛必达法则是求未定式极限的重要方法。它适用于 0/0 型或 ∞/∞ 型未定式，"
            "使用前提是分子分母同时趋于零或同时趋于无穷大。使用前必须先验证类型，"
            "否则会得出错误结论。\n\n"
            "## 3.3 连续与间断\n\n"
            "若函数在某点的极限值等于函数值，则称函数在该点连续。\n"
        ).encode("utf-8"))
        r = httpx.post(
            f"{BACKEND}/api/documents/upload",
            files={"files": ("高数-第3章.md", md, "text/markdown")},
            timeout=30,
        ).json()
        check("B1 上传成功", r["code"] == 0, r.get("message", ""))
        doc_id = r["data"]["documents"][0]["id"]
        for _ in range(40):
            d = httpx.get(f"{BACKEND}/api/documents/{doc_id}", timeout=10).json()["data"]["document"]
            if d["status"] in ("ready", "failed"):
                break
            time.sleep(0.5)
        check("B2 解析完成", d["status"] == "ready", str(d.get("error")))

        # ── C. 检索 ────────────────────────────────────
        print("[C] 混合检索")
        r = httpx.post(f"{BACKEND}/api/retrieve", json={"query": "洛必达法则"}, timeout=15).json()
        hits = r["data"]["hits"]
        check("C1 命中中文检索", len(hits) > 0, f"hits={len(hits)}")
        # MD 无页码概念（page_no=None 合法）；页码仅对 PDF/PPTX 有意义
        check("C2 页码为整数或 None（MD 无页概念）",
              all(h["page_no"] is None or isinstance(h["page_no"], int) for h in hits))
        check("C3 命中带章节/位置信息", any(h.get("section") for h in hits),
              str([h.get("section") for h in hits][:2]))

        # ── D. 问答 + 引用回填 ──────────────────────────
        print("[D] 问答与引用回填")
        conv = httpx.post(f"{BACKEND}/api/conversations", json={"mode": "normal"}, timeout=10).json()["data"]
        cid = conv["id"]
        resp_d = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid, "message": "洛必达法则的适用条件是什么？"
        }, timeout=60).json()
        if resp_d["code"] != 0:
            print("   [调试] chat 响应:", json.dumps(resp_d, ensure_ascii=False)[:500])
        ans = resp_d.get("data") or {"answer": "", "citations": [], "grounded": False}
        check("D1 回答非空", bool(ans["answer"].strip()))
        check("D2 角标已替换为 [n]", "[[c:" not in ans["answer"], ans["answer"][:120])
        check("D3 引用已回填", len(ans["citations"]) > 0)
        db_page = {h["chunk_id"]: h["page_no"] for h in hits}
        ok_pages = all(
            c["page_no"] == db_page.get(c["chunk_id"]) for c in ans["citations"]
        )
        check("D4 页码与 chunks 表一致（结构防伪）", ok_pages,
              f"citations={[(c['n'], c['page_no']) for c in ans['citations']]}")

        # ── E. 引用伪造必须被剔除 ────────────────────────
        print("[E] 引用防伪（越界角标剔除）")
        put_model("mock-empty-hits")
        cid2 = httpx.post(f"{BACKEND}/api/conversations", json={}, timeout=10).json()["data"]["id"]
        resp_e = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid2, "message": "洛必达法则怎么用？"
        }, timeout=60).json()
        if resp_e["code"] != 0:
            print("   [调试] chat 响应:", json.dumps(resp_e, ensure_ascii=False)[:400])
        ans2 = resp_e.get("data") or {"answer": "[[c:999]]", "citations": [{"n": 1}]}
        check("E1 越界角标被整体丢弃", "[[c:" not in ans2["answer"], ans2["answer"][:150])
        check("E2 不产生伪造引用", len(ans2["citations"]) == 0,
              str(ans2["citations"]))

        # ── F. 材料边界（严格模式）──────────────────────
        print("[F] 材料边界")
        put_model("mock-normal")
        cid3 = httpx.post(f"{BACKEND}/api/conversations", json={}, timeout=10).json()["data"]["id"]
        ans3 = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid3, "message": "量子纠缠的三体拓扑结构是什么？"
        }, timeout=60).json()["data"]
        check("F1 材料外明确回「未提及」", "未提及" in ans3["answer"], ans3["answer"][:150])
        check("F2 材料外零引用", len(ans3["citations"]) == 0)
        check("F3 grounded=False", ans3["grounded"] is False)

        # ── F2. 课堂提问 grounding=loose 边界（前端默认改 loose 的回归）──
        # 同一道材料外问题，loose 应允许在材料外补充并标注「材料外回答」，strict 仍如实回未提及。
        print("[F2] 课堂提问 loose 边界")
        put_model("mock-normal")
        cid_f2a = httpx.post(f"{BACKEND}/api/conversations", json={}, timeout=10).json()["data"]["id"]
        ans_f2a = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid_f2a, "message": "量子纠缠的三体拓扑结构是什么？",
            "grounding": "loose",
        }, timeout=60).json()["data"]
        check("F2.1 材料外(loose)不再回「材料中未提及」",
              "材料中未提及" not in ans_f2a["answer"], ans_f2a["answer"][:150])
        check("F2.2 材料外(loose)带「材料外回答」标注",
              "材料外回答" in ans_f2a["answer"], ans_f2a["answer"][:150])
        check("F2.3 材料外(loose)引用为空", len(ans_f2a["citations"]) == 0,
              str(ans_f2a.get("citations")))

        cid_f2b = httpx.post(f"{BACKEND}/api/conversations", json={}, timeout=10).json()["data"]["id"]
        ans_f2b = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid_f2b, "message": "量子纠缠的三体拓扑结构是什么？",
            "grounding": "strict",
        }, timeout=60).json()["data"]
        check("F2.4 材料外(strict)仍回「材料中未提及」",
              "材料中未提及" in ans_f2b["answer"], ans_f2b["answer"][:150])
        check("F2.5 材料外(strict)引用为空", len(ans_f2b["citations"]) == 0,
              str(ans_f2b.get("citations")))

        # ── G. 引导式护栏 ───────────────────────────────
        print("[G] 引导式教学护栏（20 分权重核心）")
        put_model("mock-violate-first-turn")
        cid4 = httpx.post(f"{BACKEND}/api/conversations", json={"mode": "guided"}, timeout=10).json()["data"]["id"]
        resp_g = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid4, "message": "洛必达法则怎么用？直接告诉我答案", "guided": True
        }, timeout=90).json()
        if resp_g["code"] != 0 or not resp_g.get("data") or not (resp_g["data"] or {}).get("guided"):
            print("   [调试] guided 违规模型响应:", json.dumps(resp_g, ensure_ascii=False)[:600])
        g = resp_g.get("data") or {"answer": "", "citations": [], "guided": None}
        gui = g["guided"] or {}
        check("G1 违规模型首轮 final_answer 为空", gui.get("final_answer", "") == "",
              f"source={gui.get('source')} fa={gui.get('final_answer','')[:60]}")
        check("G2 首轮拆解 ≥2 步", len(gui.get("decomposition_steps", [])) >= 2,
              str(len(gui.get("decomposition_steps", []))))
        check("G3 首轮追问 ≥1", len(gui.get("follow_up_questions", [])) >= 1)
        check("G4 conclusion_allowed=False", gui.get("conclusion_allowed") is False)
        concl = re.compile(r"答案是|正确答案是|结果是|答案为|最终答案")
        check("G5 正文无结论句式", not concl.search(g["answer"]), g["answer"][:120])
        check("G6 兜底来源可观测", gui.get("source") in ("fallback", "model_repaired", "model"),
              str(gui.get("source")))

        # 合规模型应正常通过
        put_model("mock-good-guided")
        cid5 = httpx.post(f"{BACKEND}/api/conversations", json={"mode": "guided"}, timeout=10).json()["data"]["id"]
        g2 = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid5, "message": "洛必达法则怎么用？", "guided": True
        }, timeout=90).json()["data"]
        check("G7 合规模型通过且保留引用", g2["guided"]["final_answer"] == ""
              and g2["guided"]["source"] == "model", str(g2["guided"].get("source")))

        # 非法 JSON → 兜底
        put_model("mock-bad-json")
        cid6 = httpx.post(f"{BACKEND}/api/conversations", json={"mode": "guided"}, timeout=10).json()["data"]["id"]
        g3 = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid6, "message": "极限的定义", "guided": True
        }, timeout=90).json()["data"]
        check("G8 非法 JSON 走兜底且首轮无答案", g3["guided"]["final_answer"] == ""
              and len(g3["guided"]["decomposition_steps"]) >= 2, str(g3["guided"].get("source")))

        # 盲区回写（R-F03）
        gaps = httpx.get(f"{BACKEND}/api/memories?type=knowledge_gap", timeout=10).json()["data"]
        check("G9 盲区记忆可写入/查询", isinstance(gaps["items"], list))

        # ── H. 记忆硬删除 ───────────────────────────────
        print("[H] 长期记忆（硬删除）")
        m = httpx.post(f"{BACKEND}/api/memories", json={
            "type": "preference", "content": "讲解时希望配具体例证"
        }, timeout=10).json()["data"]
        rec = httpx.post(f"{BACKEND}/api/memories/recall", json={"query": "讲解 例证"}, timeout=15).json()["data"]
        check("H1 写入后可召回", any(x["id"] == m["id"] for x in rec["items"]), str(len(rec["items"])))
        httpx.delete(f"{BACKEND}/api/memories/{m['id']}", timeout=10)
        rec2 = httpx.post(f"{BACKEND}/api/memories/recall", json={"query": "讲解 例证"}, timeout=15).json()["data"]
        check("H2 删除后不再召回（红线 #5）", not any(x["id"] == m["id"] for x in rec2["items"]))
        import sqlite3
        con = sqlite3.connect(DATA / "mrnibble.db")
        fts_rows = con.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        mem_rows = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        con.close()
        check("H3 FTS 索引行与主表一致", fts_rows == mem_rows, f"fts={fts_rows} main={mem_rows}")

        # ── I. 五类生成 + 导出 ──────────────────────────
        print("[I] 资料生成与导出")
        for model, gtype, min_n in (
            ("mock-cheatsheet", "cheatsheet", 5),
            ("mock-flashcard", "flashcard", 3),
            ("mock-mindmap", "mindmap", 1),
            ("mock-quiz", "quiz", 3),
        ):
            put_model(model)
            gid = httpx.post(f"{BACKEND}/api/generations", json={
                "type": gtype, "document_ids": [doc_id], "params": {"length": "standard", "count": 3}
            }, timeout=15).json()["data"]["generation_id"]
            gen = None
            for _ in range(60):
                gen = httpx.get(f"{BACKEND}/api/generations/{gid}", timeout=10).json()["data"]
                if gen["status"] in ("ready", "failed"):
                    break
                time.sleep(0.4)
            ok_ = gen["status"] == "ready"
            check(f"I1 {gtype} 生成成功", ok_, str(gen.get("error"))[:120])
            if not ok_:
                continue
            cj = gen["content_json"] or {}
            if gtype == "cheatsheet":
                check(f"I2 {gtype} ≥{min_n} 条目", len(cj.get("items", [])) >= min_n)
                check(f"I3 {gtype} 无伪造角标", "[[c:" not in (gen["content_md"] or ""))
            elif gtype == "mindmap":
                check(f"I2 {gtype} 根节点唯一", isinstance(cj.get("root"), dict)
                      and bool(cj["root"].get("name")))
                check(f"I3 {gtype} 可渲染 Markdown", (gen["content_md"] or "").startswith("# "))
            elif gtype == "quiz":
                items = cj.get("items", [])
                full = all(len(i["options"]) >= 2 and 0 <= i["answer_index"] < len(i["options"])
                           and i.get("explanation") for i in items)
                check(f"I2 {gtype} 四要素齐全", len(items) >= min_n and full)
            elif gtype == "flashcard":
                items = cj.get("items", [])
                check(f"I2 {gtype} 正反面齐全", len(items) >= min_n
                      and all(i.get("question") and i.get("answer") for i in items))
                fc = httpx.get(f"{BACKEND}/api/flashcards?generation_id={gid}", timeout=10).json()["data"]
                check(f"I4 {gtype} 已落库 flashcards 表", fc["total"] >= min_n)
                # 导出三件套
                for fmt, suffix in (("anki", ".apkg"), ("markdown", ".md"), ("csv", ".csv")):
                    ex = httpx.post(f"{BACKEND}/api/exports", json={
                        "generation_id": gid, "format": fmt}, timeout=30).json()
                    if ex["code"] != 0:
                        check(f"I5 导出 {fmt}", False, ex.get("message", ""))
                        continue
                    dl = httpx.get(f"{BACKEND}{ex['data']['download_url']}", timeout=30)
                    check(f"I5 导出 {fmt} 可下载且非空",
                          dl.status_code == 200 and len(dl.content) > 50
                          and ex["data"]["file_name"].endswith(suffix),
                          f"{len(dl.content)}B")
                # 目录穿越防护
                evil = httpx.get(f"{BACKEND}/api/exports/download?file=../secret.key", timeout=10).json()
                check("I6 下载路径穿越被拦截", evil["code"] != 0, json.dumps(evil, ensure_ascii=False)[:80])

        # ── J. 本地 ASR ─────────────────────────────────
        print("[J] 本地语音识别（红线：零外发）")
        st = httpx.get(f"{BACKEND}/api/asr/status", timeout=10).json()["data"]
        check("J1 模型已就位", st["available"] is True, json.dumps(st))
        wav = (ROOT / "dev" / "fixtures" / "zh_note.wav").read_bytes()
        r = httpx.post(f"{BACKEND}/api/asr/transcribe",
                       files={"audio": ("zh_note.wav", wav, "audio/wav")}, timeout=120).json()
        text = r["data"]["text"] if r["code"] == 0 else ""
        check("J2 中文语音转写成功", "思维导图" in text and "知识" in text, text)
        check("J3 返回延迟与时长", isinstance(r["data"].get("latency_ms"), int))

        # ── K. TTS 默认本地 MeloTTS ─────────────────────
        print("[K] TTS 状态")
        ts = httpx.get(f"{BACKEND}/api/tts/status", timeout=10).json()["data"]
          # 2026-09-23 用户实测：MeloTTS 效果不如系统语音（音量忽高忽低）且 CPU 合成
          # 约 40s/段，默认引擎改 system（零依赖、即时出声）；melo 保留可随时切回。
          # PRD R-G03「朗读开关默认关闭」已追加变更备注。
        check("K1 默认系统语音开启（用户 2026-09-23 要求，覆写 R-G03）",
              ts["enabled"] is True and ts["mode"] == "local" and ts.get("local_engine") == "system",
              str(ts))
        check("K2 本地朗读可用性标记", ts["local_available"] is True)
        check("K2a MeloTTS 状态字段存在", "local_engine" in ts and "local_model_available" in ts)
        speech = httpx.post(f"{BACKEND}/api/tts/speech", json={"text": "你好"}, timeout=20).json()
        check("K3 未启用云端时拒绝合成", speech["code"] == 4002, str(speech["code"]))
        # 模型是否就位两种情况都要正确处理：未下载 → JSON 错误（4000）；
        # 已下载（当前 dev 环境已随包带 int8 模型）→ 直接返回 audio/wav 二进制。
        resp = httpx.post(f"{BACKEND}/api/tts/local", json={"text": "你好"}, timeout=30)
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("audio/wav"):
            check("K4 MeloTTS 模型就位可直接合成", resp.content[:4] == b"RIFF",
                  str(resp.content[:8]))
        else:
            local_speech = resp.json()
            check("K4 MeloTTS 未下载时明确报错", local_speech["code"] == 4000,
                  str(local_speech.get("code")))

        # ── L. SSE 事件顺序 ─────────────────────────────
        print("[L] SSE 流式事件顺序")
        put_model("mock-normal")
        with httpx.stream("POST", f"{BACKEND}/api/chat/stream", json={
            "conversation_id": cid, "message": "洛必达法则的适用条件是什么？"
        }, timeout=90) as resp:
            body = resp.read().decode("utf-8")
        events = [ln[7:].strip() for ln in body.split("\n") if ln.startswith("event: ")]
        check("L1 事件序列合法", events and events[0] == "meta" and events[-1] == "done"
              and "delta" in events, str(events[:6]))
        check("L2 citation 事件存在且在 done 前", "citation" in events
              and events.index("citation") < len(events) - 1)
        deltas = re.findall(r'data: ({"text":".*?"})', body)
        check("L3 delta 为增量非空", all(json.loads(d)["text"] for d in deltas))

        # ── M. 日志脱敏 ─────────────────────────────────
        print("[M] 日志脱敏（红线 #1）")
        logs_dir = DATA / "logs"
        hit = 0
        for f in logs_dir.glob("*.log*"):
            try:
                hit += f.read_text(encoding="utf-8", errors="ignore").count(FAKE_KEY)
            except Exception:
                pass
        check("M1 日志中无 Key 明文", hit == 0, f"命中 {hit} 次")
        db_txt = (DATA / "mrnibble.db").read_bytes()
        check("M2 DB 中 Key 为密文", FAKE_KEY.encode() not in db_txt)

        # ── N. 重新解析（换嵌入模型后重建索引）──────────
        print("[N] 重新解析")

        def chunk_count() -> int:
            con = sqlite3.connect(DATA / "mrnibble.db")
            try:
                return int(con.execute(
                    "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (doc_id,)
                ).fetchone()[0])
            finally:
                con.close()

        before = chunk_count()
        rp = httpx.post(f"{BACKEND}/api/documents/{doc_id}/reparse", timeout=180).json()
        check("N1 reparse 接口成功", rp["code"] == 0, rp.get("message", ""))
        after_doc = (rp.get("data") or {}).get("document") or {}
        check("N2 重新解析后状态仍为 ready", after_doc.get("status") == "ready",
              str(after_doc.get("status")))
        after = chunk_count()
        check("N3 旧切片被清空重建（未累积翻倍）", after == before and before > 0,
              f"before={before} after={after}")
        r2 = httpx.post(f"{BACKEND}/api/retrieve", json={"query": "洛必达法则"}, timeout=15).json()
        check("N4 重建索引后仍可检索", len(r2["data"]["hits"]) > 0, str(len(r2["data"]["hits"])))
        # 重新解析必须换用「当前」嵌入通道：向量维度应等于当前 provider 的维度
        cur_dim = None
        con = sqlite3.connect(DATA / "mrnibble.db")
        try:
            row = con.execute(
                "SELECT embedding_dim FROM chunks WHERE document_id = ? AND embedding IS NOT NULL "
                "LIMIT 1", (doc_id,)
            ).fetchone()
            cur_dim = row[0] if row else None
        finally:
            con.close()
        check("N5 切片带嵌入向量", cur_dim is not None and int(cur_dim) > 0, f"dim={cur_dim}")

        # ── O. 模型配置完整性（2026-09-12 新增：用户实际踩到的坑）────
        # 现象：只填了 base_url 没填模型名 → 连接测试「成功」但界面显示未配置模型，
        # 用户不知道自己漏了一项。以下断言守住该问题的三个修复点。
        print("[O] 模型配置完整性与模型名校验")
        r = httpx.get(f"{BACKEND}/api/settings/models?target=llm", timeout=20).json()
        models = r["data"]["models"]
        check("O1 可拉取端点可用模型列表", "mock-normal" in models, str(models[:4]))

        put_model("mock-normal")
        r = httpx.post(f"{BACKEND}/api/settings/test", json={"target": "llm"}, timeout=20).json()
        check("O2 模型名在列表中 → 无告警", r["code"] == 0 and not r["data"].get("warning"),
              str(r["data"].get("warning"))[:80])

        put_model("不存在的模型-xyz")
        r = httpx.post(f"{BACKEND}/api/settings/test", json={"target": "llm"}, timeout=20).json()
        warn = (r.get("data") or {}).get("warning") or ""
        check("O3 模型名不在列表中 → 明确告警", r["code"] == 0 and "不在该端点的可用列表" in warn,
              warn[:90])

        put_model("")
        r = httpx.post(f"{BACKEND}/api/settings/test", json={"target": "llm"}, timeout=20).json()
        check("O4 模型名留空 → 拒绝并指出缺模型名",
              r["code"] == 2000 and "模型名" in r["message"], f"{r['code']} {r['message']}")

        r = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": cid, "message": "测试"
        }, timeout=30).json()
        blob = r["message"] + " " + ((r.get("error") or {}).get("detail") or "")
        check("O5 半配置状态下问答报错含缺失项",
              r["code"] == 2000 and "模型名" in blob, f"{r['code']} {blob[:80]}")
        put_model("mock-normal")

        # ── P. 材料概览兜底（「上传了文件却像没读进去」的场景）──────
        # 现象：文件名/文档级问法与正文没有词汇交集 → 关键词与向量双通道 0 召回
        # → 提示词里没有材料 → 模型回「我没有收到文件，请把内容粘贴进来」。
        # 修复：检索为空且问题指向材料本身时，注入「标题 + 章节大纲 + 开头片段」。
        print("[P] 材料概览兜底")
        put_model("mock-echo-context")  # 回显注入的 system 上下文，直接看到材料是否进提示词

        def chat_in(doc_ids, message: str) -> dict:
            c = httpx.post(f"{BACKEND}/api/conversations",
                           json={"mode": "normal", "document_ids": doc_ids},
                           timeout=10).json()["data"]
            return httpx.post(f"{BACKEND}/api/chat", json={
                "conversation_id": c["id"], "message": message}, timeout=60).json()

        for label, msg in (
            ("文件名问法", "看下高数-第3章.md的具体内容，然后安排学习"),
            ("文档级问法", "帮我总结一下这篇文档"),
            ("课程开场", "开始学习"),
        ):
            rr = chat_in([doc_id], msg)
            ans_p = (rr.get("data") or {}).get("answer") or rr.get("message") or ""
            check(f"P {label} → 材料已进提示词",
                  rr["code"] == 0 and "【材料概览】" in ans_p and "[材料1]" in ans_p,
                  ans_p[:100])
        rr = chat_in([doc_id], "帮我总结一下这篇文档")
        ans_p = (rr.get("data") or {}).get("answer") or ""
        check("P4 概览含真实章节大纲（来自 DB 的 section）",
              "章节结构" in ans_p and "3.2 洛必达法则" in ans_p,
              ans_p[:160].replace("\n", " "))

        # 红线不能破：材料外问题既没提文件名、也不是文档级问法 → 仍走确定性回复
        rr = chat_in([doc_id], "量子纠缠的三体拓扑结构是什么？")
        d_out = rr.get("data") or {}
        check("P5 材料外问题（已选定材料）仍回未提及且零引用",
              "材料中未提及" in (d_out.get("answer") or "")
              and not (d_out.get("citations") or []),
              (d_out.get("answer") or "")[:80])
        rr = chat_in(None, "量子纠缠的三体拓扑结构是什么？")
        d_out = rr.get("data") or {}
        check("P6 材料外问题（未选定材料）仍回未提及",
              "材料中未提及" in (d_out.get("answer") or ""), (d_out.get("answer") or "")[:80])

        # 用户实际用的是引导式：这条路径也必须拿到材料概览
        put_model("mock-echo-guided")
        c_g = httpx.post(f"{BACKEND}/api/conversations",
                         json={"mode": "guided", "document_ids": [doc_id]},
                         timeout=10).json()["data"]
        rr = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": c_g["id"], "message": "看下高数-第3章.md的内容，然后安排学习",
            "guided": True}, timeout=60).json()
        d_out = rr.get("data") or {}
        gui_p = d_out.get("guided") or {}
        check("P7 引导式路径同样注入材料概览",
              rr["code"] == 0 and "【材料概览】" in (d_out.get("answer") or "")
              and "[材料1]" in (d_out.get("answer") or ""),
              (d_out.get("answer") or "")[:90])
        check("P8 引导式首轮仍不给答案（概览不破坏护栏）",
              gui_p.get("final_answer", "") == "" and gui_p.get("conclusion_allowed") is False,
              f"fa={gui_p.get('final_answer', '')[:40]}")
        put_model("mock-normal")

        # ── Q. 引导式「原地打转」防护（用户实测：一直重复问同一个问题）──
        print("[Q] 引导式推进与消息顺序")
        sys.path.insert(0, str(ROOT / "src"))
        from backend.models.guided import GuidedOutput          # noqa: E402
        from backend.services.guardrails import Guardrail       # noqa: E402
        from backend.services.guided import GuidedService       # noqa: E402

        # Q1/Q2 单元级：状态推导必须识别「学生在回答」，护栏必须识别「原地打转」
        hist_asked = [{"role": "assistant", "content_json": {
            "mode": "explain", "follow_up_questions": ["中文路径有问题吗？"],
            "next_action": "ask_follow_up", "summary": "先想想。"}}]
        check("Q1 学生答完后状态推为 EVALUATE",
              GuidedService.derive_state(hist_asked) == "EVALUATE",
              GuidedService.derive_state(hist_asked))
        same = GuidedOutput.model_validate({
            "mode": "explain", "final_answer": "",
            "decomposition_steps": [{"step": 1, "title": "A", "hint": ""},
                                    {"step": 2, "title": "B", "hint": ""}],
            "follow_up_questions": ["中文路径有问题吗？"],
            "next_action": "ask_follow_up", "summary": "先想想。"})
        check("Q2 原地打转被护栏识别",
              bool(Guardrail.check(same, False, same)), str(Guardrail.check(same, False, same)))

        # Q3 端到端：模型恒定输出同一份内容时，第二轮必须换措辞（不能复读）
        put_model("mock-stall-guided")
        c_s = httpx.post(f"{BACKEND}/api/conversations",
                         json={"mode": "guided", "document_ids": [doc_id]},
                         timeout=10).json()["data"]

        def guided_turn(msg: str) -> dict:
            return httpx.post(f"{BACKEND}/api/chat", json={
                "conversation_id": c_s["id"], "message": msg, "guided": True},
                timeout=60).json()["data"]

        t1 = guided_turn("开始学习")
        t2 = guided_turn("中文路径本身没问题吧")
        gui2 = t2.get("guided") or {}
        q1 = (t1.get("guided") or {}).get("follow_up_questions") or []
        q2 = gui2.get("follow_up_questions") or []
        check("Q3 第二轮不再复读第一轮的追问", q1 and q2 and q1 != q2,
              f"t1={q1[:1]} t2={q2[:1]}")
        check("Q4 第二轮正文与第一轮不同",
              (t1.get("answer") or "") != (t2.get("answer") or ""),
              (t2.get("answer") or "")[:60])
        check("Q5 打转时走兜底并可观测", gui2.get("source") == "fallback",
              str(gui2.get("source")))

        # Q6 消息顺序：历史在提问之前，学生当前输入必须是最后一条
        put_model("mock-echo-guided")
        c_o = httpx.post(f"{BACKEND}/api/conversations",
                         json={"mode": "guided", "document_ids": [doc_id]},
                         timeout=10).json()["data"]
        httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": c_o["id"], "message": "开始学习", "guided": True}, timeout=60)
        t = httpx.post(f"{BACKEND}/api/chat", json={
            "conversation_id": c_o["id"], "message": "中文路径本身没问题吧",
            "guided": True}, timeout=60).json()["data"]
        ans_q = t.get("answer") or ""
        roles = ""
        if "roles=" in ans_q:
            roles = ans_q.split("roles=", 1)[1].split("]", 1)[0]
        check("Q6 消息顺序：历史在前、学生输入在最后",
              roles.endswith("user") and "assistant" in roles[:-len("user")] and "system" in roles,
              f"roles={roles}")
        put_model("mock-normal")

    finally:
        for p in (be, mock):
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败清单：")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
