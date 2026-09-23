"""课程生成质量评测（端到端，真实模型）。

用途：用真实模型完整跑一遍「上传材料 → 生成大纲 → 逐讲生成讲义 → 无头浏览器
渲染验证」，并产出可量化的质量数据（页数/可视化产物/讲稿/引用/服务端防线痕迹）。

与 `dev/course_check.py` 的区别：
  - course_check 用 **mock 模型桩**，验证的是「代码路径对不对」，几秒钟跑完、结果确定；
  - 本脚本用**真实模型**，验证的是「产出的课件质量好不好」，
    要几分钟，且**结果不稳定**（同一材料多跑几次，可视化产出数量会明显波动）。
  两者互补：前者做回归，后者做质量抽查。

用法：
    .venv/Scripts/python.exe dev/course_quality_eval.py
    .venv/Scripts/python.exe dev/course_quality_eval.py --max-lessons 1     # 快速抽查
    .venv/Scripts/python.exe dev/course_quality_eval.py --units 3
    .venv/Scripts/python.exe dev/course_quality_report.py                 # 出 Markdown 报告

设计约束（踩过的坑，别改）：
  1. 沙箱会回收工具调用内拉起的进程 → 全流程必须**单脚本一次跑完**。
  2. `data/browser-profile` 有 Chrome 锁定的文件、且文件数远超批量删除阈值 →
     **绝不 copytree 整个 data/**，只复制 mrnibble.db / secret.key。
  3. 复制过来的库要清空业务表，否则新上传的测试材料会被去重跳过。
  4. 用**源码后端 + 独立端口**，不 taskkill 用户正在运行的啃书先生.exe。
  5. 目录名带时间戳、**不做删除**（rmtree 会被批量删除保护拦下）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent          # mrnibble/
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LAUNCHER = Path(__file__).resolve().parent / "eval_backend_launcher.py"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEFAULT_MATERIAL = Path(r"Q:\xiaolongxia\xiaxia\hyperknow调研\学习好文用于测试的文件.txt")


def newest_dist() -> Path:
    """取最新的 distN 构建（评测需要用户真实的模型与 Key，从在用副本里读）。

    不把 dist 号写死：每次构建后 dist 号会递增、旧目录会被清理。
    """
    cands = [p for p in ROOT.glob("dist*") if (p / "啃书先生" / "data" / "mrnibble.db").exists()]
    if not cands:
        return ROOT / "dist24" / "啃书先生"
    def _num(p: Path) -> int:
        m = re.search(r"(\d+)$", p.name)
        return int(m.group(1)) if m else 0
    return sorted(cands, key=_num)[-1] / "啃书先生"

# 保留（配置类）；其余一律清空（内容类）
_KEEP_TABLES = {"settings", "schema_meta", "sqlite_sequence"}

LOG_LINES: list[str] = []


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_LINES.append(line)


# ── 隔离环境 ────────────────────────────────────────────────
def _reset_business_tables(db_path: Path) -> None:
    """把复制来的用户库清成「只有配置、没有业务数据」。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        cleared = 0
        for n in names:
            if n in _KEEP_TABLES or n.startswith("sqlite_"):
                continue
            try:
                conn.execute(f"DELETE FROM {n}")
                cleared += 1
            except Exception:  # noqa: BLE001
                pass
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
        # FTS5 外部内容表：清完主表后 rebuild，避免残留索引指向已删行
        for fts in ("chunks_fts", "memories_fts"):
            try:
                conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
            except Exception:  # noqa: BLE001
                pass
        conn.commit()
        say(f"    已清空 {cleared} 张内容表，保留 settings")
    finally:
        conn.close()


def prepare_env(dist: Path) -> tuple[Path, Path, Path]:
    """准备隔离数据目录。返回 (WORK, DATA, RECEIPTS)。"""
    work = ROOT / ".tmp" / f"e2e24-{time.strftime('%H%M%S')}"
    data = work / "data"
    data.mkdir(parents=True, exist_ok=True)
    src = dist / "data"
    for name in ("mrnibble.db", "secret.key"):
        if (src / name).exists():
            shutil.copy2(src / name, data / name)
    dbf = data / "mrnibble.db"
    if dbf.exists():
        _reset_business_tables(dbf)
    else:
        say(f"⚠ {dist} 下没有 mrnibble.db —— 将用全新空库（需要自己配模型与 Key）")
    say(f"配置来源：{src}")
    say(f"隔离数据目录就绪（已清空业务数据）：{data}")
    return work, data, work / "receipts.jsonl"


# ── 分析工具 ────────────────────────────────────────────────
def chinese_len(s: str) -> int:
    return len(re.sub(r"\s+", "", str(s or "")))


def cjk_ok(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(s or "")))


def analyze_lesson(detail: dict) -> dict:
    slides = detail.get("slides") or []
    scripts = detail.get("scripts") or []
    board = detail.get("board") or {}
    cards = (board.get("cards") if isinstance(board, dict) else None) or []

    kinds: dict[str, int] = {}
    viz_pages: list[dict] = []
    cits = 0
    no_cjk = 0
    long_bullets = 0

    for s in slides:
        k = str(s.get("kind") or "?")
        kinds[k] = kinds.get(k, 0) + 1
        cits += len(s.get("citation_refs") or [])
        for b in (s.get("bullets") or []):
            if chinese_len(b) > 30:
                long_bullets += 1
        if not cjk_ok(s.get("title")) and not cjk_ok(" ".join(s.get("bullets") or [])):
            no_cjk += 1
        dg, ch, tb, tk = s.get("diagram"), s.get("chart"), s.get("table"), s.get("takeaway")
        if not (dg or ch or tb or tk):
            continue
        item = {"id": s.get("id"), "kind": k, "title": s.get("title")}
        if dg:
            ir = dg.get("ir") or {}
            svg = str(dg.get("svg") or "")
            item.update({
                "viz": "diagram",
                "form": "ir+svg" if svg else ("mermaid" if dg.get("code") else "?"),
                "diagram_type": ir.get("diagram_type"),
                "preset": (ir.get("meta") or {}).get("preset"),
                "svg_bytes": len(svg),
                "svg_nodes": svg.count("zf-node"),
                "svg_edges": svg.count("zf-edge"),
                "node_count": len(ir.get("nodes") or ir.get("components")
                                   or ir.get("participants") or ir.get("states") or []),
                "ir_full": ir,
            })
        elif ch:
            item.update({"viz": "chart", "chart_type": ch.get("type"),
                         "cats": len(ch.get("categories") or []),
                         "series": len(ch.get("series") or [])})
        elif tb:
            item.update({"viz": "table", "cols": len(tb.get("columns") or []),
                         "rows": len(tb.get("rows") or [])})
        else:
            item.update({"viz": "takeaway", "len": chinese_len(tk)})
        viz_pages.append(item)

    by_sid = {str(sc.get("slide_id") or ""): sc for sc in scripts}
    mirror, stats = 0, []
    for s in slides:
        sc = by_sid.get(str(s.get("id")))
        if not sc:
            continue
        txt = str(sc.get("text") or "")
        sl_txt = str(s.get("title") or "") + "".join(s.get("bullets") or [])
        ratio = round(chinese_len(txt) / max(1, chinese_len(sl_txt)), 2)
        if ratio < 1.5:
            mirror += 1
        stats.append({"slide": s.get("id"), "chars": chinese_len(txt), "ratio": ratio})
    lens = [x["chars"] for x in stats] or [0]

    return {
        "lesson_id": detail.get("id"), "title": detail.get("title"),
        "kind": detail.get("kind"), "objective": detail.get("objective"),
        "depth": detail.get("depth"),
        "slides_n": len(slides), "scripts_n": len(scripts), "cards_n": len(cards),
        "slide_kinds": kinds, "viz_pages": viz_pages, "viz_n": len(viz_pages),
        "citations_n": cits, "no_cjk_slides": no_cjk, "bullets_over_30": long_bullets,
        "script_min": min(lens), "script_max": max(lens),
        "script_avg": round(sum(lens) / max(1, len(lens)), 1),
        "mirror_suspects": mirror,
        "recap": chinese_len(board.get("recap") if isinstance(board, dict) else ""),
        "summary": chinese_len(board.get("summary") if isinstance(board, dict) else ""),
    }


def dump_dom(url: str) -> str:
    out = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-proxy-server",
         "--virtual-time-budget=9000", "--dump-dom", url],
        capture_output=True, timeout=120, encoding="utf-8", errors="replace")
    return out.stdout or ""


# ── 主流程 ──────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="课程生成质量评测（真实模型）")
    ap.add_argument("--material", default=str(DEFAULT_MATERIAL), help="材料文件路径")
    ap.add_argument("--goal", default="掌握材料里的核心内容，能解释原理并完成配套练习")
    ap.add_argument("--units", type=int, default=2, help="大纲单元数")
    ap.add_argument("--max-lessons", type=int, default=0,
                    help="最多生成几讲讲义（0 = 全部讲义类讲次）")
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--dist", default="", help="从哪个构建目录取用户配置（默认取最新的 distN）")
    ap.add_argument("--out-json", default=str(ROOT / ".tmp" / "e2e24-report.json"))
    args = ap.parse_args()

    material = Path(args.material)
    if not material.exists():
        print(f"材料不存在：{material}")
        return 1

    base = f"http://127.0.0.1:{args.port}"
    C = dict(trust_env=False, timeout=180)

    dist = Path(args.dist) if args.dist else newest_dist()
    work, data, receipts = prepare_env(dist)
    log_path = work / "backend.log"
    logf = open(log_path, "w", encoding="utf-8")
    # 用包装启动器：拦截 diagram.compile_ir，把真实回执（规则码 + 失败 IR）落盘
    proc = subprocess.Popen([str(PY), str(LAUNCHER)], cwd=str(ROOT),
                            stdout=logf, stderr=subprocess.STDOUT,
                            env={**os.environ, "MRNIBBLE_DATA_DIR": str(data),
                                 "MRNIBBLE_PORT": str(args.port), "PYTHONPATH": str(ROOT / "src"),
                                 "E2E_RECEIPTS": str(receipts),
                                 "PYTHONIOENCODING": "utf-8"})
    result: dict = {"ok": False, "stages": {}, "lessons": [], "errors": [],
                    "material": str(material), "work_dir": str(work)}
    try:
        for _ in range(240):
            try:
                if httpx.get(f"{base}/api/health", **C).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            say("✗ 后端未启动")
            result["errors"].append("backend not up")
            return 1
        cfg = httpx.get(f"{base}/api/settings", **C).json()["data"]
        result["model"] = cfg["llm"]["model"]
        result["base_url"] = cfg["llm"]["base_url"]
        say(f"后端就绪 | 模型 = {cfg['llm']['model']} | Key 已配 = {cfg['llm']['api_key_set']}")

        # 1) 材料
        r = httpx.post(f"{base}/api/documents/upload", **C,
                       files={"files": (material.name, material.read_bytes(), "text/plain")}).json()
        if not (r.get("data") or {}).get("documents"):
            say(f"✗ 上传未返回文档：{json.dumps(r, ensure_ascii=False)[:300]}")
            result["errors"].append(f"upload: {json.dumps(r, ensure_ascii=False)[:200]}")
            return 1
        doc = r["data"]["documents"][0]["id"]
        say(f"材料已上传：{material.name}（{material.stat().st_size} 字节）")
        t0 = time.time()
        while time.time() - t0 < 240:
            d = httpx.get(f"{base}/api/documents/{doc}", **C).json()["data"]["document"]
            if d["status"] in ("ready", "failed"):
                break
            time.sleep(1)
        result["stages"]["document"] = {"status": d["status"], "secs": round(time.time() - t0, 1)}
        say(f"材料解析：{d['status']}（{time.time() - t0:.1f}s）")
        if d["status"] != "ready":
            result["errors"].append(f"document {d['status']}")
            return 1

        # 2) 大纲
        say("── 生成大纲 ──")
        r = httpx.post(f"{base}/api/courses", **C, json={
            "goal": args.goal, "document_ids": [doc], "unit_count": args.units}).json()
        cid, jid = r["data"]["course_id"], r["data"]["job_id"]
        t0 = time.time()
        while time.time() - t0 < 900:
            j = httpx.get(f"{base}/api/courses/jobs/{jid}", **C).json()["data"]
            if j.get("status") in ("ready", "failed"):
                break
            time.sleep(1.5)
        result["stages"]["outline"] = {"status": j.get("status"),
                                      "secs": round(time.time() - t0, 1),
                                      "error": j.get("error")}
        say(f"大纲：{j.get('status')}（{time.time() - t0:.0f}s）")
        if j.get("status") != "ready":
            result["errors"].append(f"outline {j.get('status')}: {j.get('error')}")
            return 1

        course = httpx.get(f"{base}/api/courses/{cid}", **C).json()["data"]
        units = course["units"]
        lessons = [l for u in units for l in u["lessons"]]
        result["outline"] = {
            "title": course.get("title"), "summary": course.get("summary"),
            "course_depth": course.get("depth"), "level": course.get("level"),
            "units": [{"title": u["title"], "summary": u.get("summary"),
                       "lessons": [{"title": l["title"], "kind": l.get("kind"),
                                    "depth": l.get("depth"), "objective": l.get("objective")}
                                   for l in u["lessons"]]} for u in units],
            "lessons_n": len(lessons),
        }
        say(f"大纲产出：{len(units)} 单元 / {len(lessons)} 讲")
        for u in units:
            say(f"  · {u['title']}")
            for l in u["lessons"]:
                say(f"      - [{l.get('kind')}/{l.get('depth')}] {l['title']}")

        # 3) 逐讲讲义
        lect = [l for l in lessons if str(l.get("kind")) == "lecture"]
        if args.max_lessons:
            lect = lect[:args.max_lessons]
        say(f"── 逐讲生成讲义（{len(lect)} 讲，真实模型耗时不可控）──")
        for i, l in enumerate(lect, 1):
            say(f"  第 {i}/{len(lect)} 讲：{l['title']}")
            try:
                rr = httpx.post(f"{base}/api/courses/lessons/{l['id']}/lecture", **C).json()
                if rr.get("code") != 0:
                    result["errors"].append(f"lecture {l['id']}: {str(rr)[:200]}")
                    continue
                jid2 = rr["data"]["job_id"]
                t0, last = time.time(), ""
                while time.time() - t0 < 900:
                    jj = httpx.get(f"{base}/api/courses/jobs/{jid2}", **C).json()["data"]
                    st = str(jj.get("stage") or "")
                    if st and st != last:
                        say(f"    · stage → {st}（{time.time() - t0:.0f}s）")
                        last = st
                    if jj.get("status") in ("ready", "failed"):
                        break
                    time.sleep(1.5)
                if jj.get("status") != "ready":
                    result["errors"].append(f"lecture {l['id']} {jj.get('status')}: {jj.get('error')}")
                    say(f"    ✗ {jj.get('status')} {jj.get('error')}")
                    continue
                detail = httpx.get(f"{base}/api/courses/lessons/{l['id']}", **C).json()["data"]
                an = analyze_lesson(detail)
                an["gen_secs"] = round(time.time() - t0, 1)
                result["lessons"].append(an)
                say(f"    ✓ {an['slides_n']} 页 / {an['scripts_n']} 段讲稿 / "
                    f"{an['cards_n']} 卡片 / 可视化 {an['viz_n']} 页（{an['gen_secs']}s）")
            except Exception as e:  # noqa: BLE001
                result["errors"].append(f"lecture {l['id']} 异常: {type(e).__name__}: {e}")
                say(f"    ✗ 异常 {type(e).__name__}")

        # 4) 渲染验证
        say("── 渲染验证（无头 Chrome）──")
        render = []
        for l in lect:
            if not any(x["lesson_id"] == l["id"] for x in result["lessons"]):
                continue
            try:
                dom = dump_dom(f"{base}/#/lessons/{l['id']}")
            except Exception as e:  # noqa: BLE001
                render.append({"lesson_id": l["id"], "error": type(e).__name__})
                continue
            item = {"lesson_id": l["id"], "title": l["title"],
                    "cards": dom.count('class="board-card'),
                    "viz_box": dom.count('class="viz-box"'),
                    "inline_svg": dom.count("zf-svg"),
                    "svg_tag": dom.count("<svg"),
                    "chart_canvas": dom.count("chart-box"),
                    "viz_table": dom.count("viz-table"),
                    "takeaway_box": dom.count("takeaway-box"),
                    "fallback": dom.count("viz-fallback"),
                    "markmap": dom.count("markmap")}
            render.append(item)
            say(f"  · {l['title']}: 卡片{item['cards']} viz-box{item['viz_box']} "
                f"内联svg{item['inline_svg']} 表{item['viz_table']} 金句{item['takeaway_box']} "
                f"回退{item['fallback']}")
        result["render"] = render
        result["ok"] = True
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=12)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        logf.close()
        # 回执
        try:
            lines = receipts.read_text(encoding="utf-8").splitlines() if receipts.exists() else []
            result["diagram_receipts"] = [json.loads(x) for x in lines if x.strip()]
            bad = [x for x in result["diagram_receipts"] if not x.get("ok")]
            if bad:
                say(f"捕获 {len(bad)} 次图示校验失败："
                    + ", ".join(f"{x.get('stage')}/{[d.get('code') for d in (x.get('diagnostics') or [])][:2]}"
                                for x in bad))
            else:
                n = len(result["diagram_receipts"])
                say(f"图示编译器被调用 {n} 次，"
                    + ("全部一次通过" if n else "本次没有任何图示页（模型未产出 diagram）"))
        except Exception as e:  # noqa: BLE001
            result["diagram_receipts"] = []
            say(f"读取回执失败：{type(e).__name__}")
        # 日志与防线痕迹
        try:
            txt = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            txt = ""
        for kw in ("退化", "剥除", "回执", "重写", "降级", "失败", "未通过校验", "引用为空", "超限"):
            n = txt.count(kw)
            if n:
                result.setdefault("backend_log_hits", {})[kw] = n
        result["backend_log"] = txt
        result["run_id"] = time.strftime("%m%d-%H%M%S")
        # 只生成部分讲次的抽查（--max-lessons）不进对照样本：样本量不同没法横向比
        result["partial"] = bool(args.max_lessons)
        out = Path(args.out_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        # 按时间戳留存（模型采样有随机性，需要多轮样本才能看出稳定性）
        runs = ROOT / ".tmp" / "e2e-runs"
        runs.mkdir(parents=True, exist_ok=True)
        if not result["partial"]:
            (runs / f"run-{result['run_id']}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        say(f"结果已落盘：{out}（样本 {result['run_id']}"
            + ("，部分讲次、不计入对照样本）" if result["partial"] else "）"))
        say(f"隔离数据保留在：{work}（含生成的课程，可打开页面查看）")


if __name__ == "__main__":
    sys.exit(main())
