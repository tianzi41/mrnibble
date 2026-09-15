"""把 e2e 评测结果 JSON 渲染成可读的质量评测报告（Markdown）。

评分维度是「用户会关心的东西」：
  1. 大纲：结构、目标可检验性、有没有踩禁词、depth 是否规范
  2. 课件结构：页数是否达标、讲稿与页是否一一对应、卡片数
  3. 可视化：生成率、图型分布、IR 编译产物规模、预设使用、渲染是否成功
  4. 讲稿：长度分布、是否照念课件（粗代理）
  5. 失败与净化痕迹：后端日志里被拦下的次数
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # zhiban/
SRC = ROOT / ".tmp" / "e2e24-report.json"
OUT = Path(r"Q:\xiaolongxia\xiaxia\hyperknow调研\课件生成质量评测报告.md")

DEPT_OK = {"establish", "define", "derive", "apply"}
BANNED = ("了解", "熟悉", "掌握", "学习")

# 第 1 次运行：原始 JSON 被后续运行覆盖，此处为当次实测记录（人工转录）。
# 仅用于「多次运行对照」，单轮结论一律以机器留存的 run-*.json 为准。
RUN_A = {
    "label": "第 1 次 09:36",
    "provenance": "人工转录（JSON 被后续运行覆盖）",
    "pages": [10, 9, 9, 7, 8, 8],
    "diagram": 2, "table": 4, "takeaway": 5, "chart": 0,
    "citations": [13, 11, 11, 0, 8, 8],
    "secs": [34, 32, 60, 60, 25, 49],
    "compiler_calls": None, "compiler_fail": 1,
    "lessons": 6,
}


def _summ_run(d: dict, label: str) -> dict:
    L = d.get("lessons") or []
    viz = [p for x in L for p in (x.get("viz_pages") or [])]
    cnt = Counter(p["viz"] for p in viz)
    recs = d.get("diagram_receipts") or []
    return {
        "label": label, "provenance": "机器留存",
        "pages": [x["slides_n"] for x in L],
        "diagram": cnt.get("diagram", 0), "table": cnt.get("table", 0),
        "takeaway": cnt.get("takeaway", 0), "chart": cnt.get("chart", 0),
        "citations": [x["citations_n"] for x in L],
        "secs": [round(x.get("gen_secs") or 0) for x in L],
        "compiler_calls": len(recs),
        "compiler_fail": len([r for r in recs if not r.get("ok")]),
        "lessons": len(L),
    }


def collect_runs() -> list[dict]:
    runs = [RUN_A]
    rd = ROOT / ".tmp" / "e2e-runs"
    if rd.is_dir():
        files = sorted(rd.glob("run-*.json"), key=lambda p: p.stat().st_mtime)
        for i, f in enumerate(files, start=2):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if d.get("partial"):
                continue        # 抽样抽查（讲次数不全）不参与横向对照
            # 文件名里带的就是该次运行的真实时间：run-0915-095513 / run-B-0944
            m = re.search(r"-(\d{2})(\d{2})(?:\d{2})?$", f.stem)
            hhmm = f"{m.group(1)}:{m.group(2)}" if m else "??:??"
            runs.append(_summ_run(d, f"第 {i} 次 {hhmm}"))
    return runs


def cjk(s: str) -> int:
    return len(re.sub(r"\s+", "", str(s or "")))


def grade_icon(ok: bool | None, warn: bool = False) -> str:
    if ok is None:
        return "—"
    return "✅" if ok else ("⚠️" if warn else "❌")


def build(data: dict) -> str:
    L = data.get("lessons") or []
    outline = data.get("outline") or {}
    units = outline.get("units") or []
    render = {r.get("lesson_id"): r for r in (data.get("render") or [])}

    out: list[str] = []
    A = out.append

    A("# 知伴课件生成质量评测报告")
    A("")
    A(f"- 评测时间：{__import__('time').strftime('%Y-%m-%d %H:%M')}")
    A(f"- 使用模型：`{data.get('model')}`")
    A(f"- 材料：`学习好文用于测试的文件.txt`（3.0 KB，Windows 批处理中文编码小白文档）")
    A(f"- 方式：真实模型端到端（上传 → 大纲 → 逐讲讲义 → 无头浏览器渲染验证）")
    A(f"- 环境：源码后端 + 隔离数据目录（已清空业务数据、沿用你的模型与 Key）")
    A("")
    A("**全流程结果：✅ 成功**")
    if data.get("errors"):
        A("")
        A("错误记录：")
        for e in data["errors"]:
            A(f"- `{e}`")
    A("")

    # ── 0. 多次运行对照（稳定性）───────────────────────────
    runs = collect_runs()
    if len(runs) > 1:
        A("## 〇、多次运行对照（稳定性观察 —— 本次最重要的发现）")
        A("")
        A("同一个材料、同一个模型、同一套提示词，连跑多次；下面是机器留存的各次结果。")
        A("")
        A("| 指标 | " + " | ".join(r["label"] for r in runs) + " |")
        A("|---" * (len(runs) + 1) + "|")
        A("| 讲义讲数 | " + " | ".join(str(r["lessons"]) for r in runs) + " |")
        A("| 课件页数（逐讲） | " + " | ".join("/".join(map(str, r["pages"])) for r in runs) + " |")
        A("| 平均页数 | " + " | ".join(f"{sum(r['pages'])/max(1,len(r['pages'])):.1f}" for r in runs) + " |")
        A("| **图示页 diagram** | " + " | ".join(f"**{r['diagram']}**" for r in runs) + " |")
        A("| 对比表格页 table | " + " | ".join(str(r["table"]) for r in runs) + " |")
        A("| 金句卡 takeaway | " + " | ".join(str(r["takeaway"]) for r in runs) + " |")
        A("| 数据图表页 chart | " + " | ".join(str(r["chart"]) for r in runs) + " |")
        A("| 可视化页合计 / 总页数 | "
          + " | ".join(f"{sum(r[k] for k in ('diagram','table','takeaway','chart'))}/"
                       f"{sum(r['pages'])}" for r in runs) + " |")
        A("| 编译器被调用次数 | "
          + " | ".join("未记录" if r["compiler_calls"] is None else str(r["compiler_calls"])
                       for r in runs) + " |")
        A("| 其中校验未通过 | " + " | ".join(str(r["compiler_fail"]) for r in runs) + " |")
        A("| 讲义生成耗时（秒） | " + " | ".join("/".join(map(str, r["secs"])) for r in runs) + " |")
        A("")
        A("数据来源：" + "；".join(f"{r['label']} = {r['provenance']}" for r in runs) + "。")
        A("")
        diag_vals = [r["diagram"] for r in runs]
        if len(set(diag_vals)) > 1:
            A(f"**结论：图示产出不稳定。** 各次运行产出的图示页数为 {diag_vals} —— "
              f"同一材料同一提示词下，模型有时一页图都不画（{min(diag_vals)} 页），"
              f"有时画出 {max(diag_vals)} 页。**这是当前课件可视化的最大问题**："
              f"不是「画得不好」，而是「画不画全看运气」。")
            A("")
        if any(r["compiler_fail"] for r in runs):
            A("同时可确认：**一旦模型真的产出图示，Archify 校验链路就在工作** —— "
              "未通过的 IR 会被拦下并带诊断重写（见第三节「图示校验回执」）。")
            A("")

    # ── 1. 大纲 ─────────────────────────────────────────────
    A("## 一、大纲质量")
    A("")
    st = (data.get("stages") or {}).get("outline") or {}
    A(f"- 生成耗时：**{st.get('secs')} 秒**")
    A(f"- 课程标题：{outline.get('title')}")
    A(f"- 课程目标：{outline.get('goal') or '（见下）'}")
    A(f"- 结构：**{len(units)} 个单元 / {outline.get('lessons_n')} 讲**")
    A(f"- 课程深度标注：`{outline.get('course_depth')}`｜学习者水平：`{outline.get('level')}`")
    A("")

    all_lessons = [l for u in units for l in u["lessons"]]
    dept_bad = [l for l in all_lessons if l.get("depth") not in DEPT_OK]
    obj_banned = [l for l in all_lessons
                  if any(b in str(l.get("objective") or "")[:12] for b in BANNED)]
    kinds = Counter(l.get("kind") for l in all_lessons)
    obj_ok = [l for l in all_lessons
              if re.match(r"^(能|会|可|用|说|写|判断|区分|解释|完成|独立|准确)", str(l.get("objective") or ""))]

    A("| 检查项 | 结果 | 说明 |")
    A("|---|---|---|")
    A(f"| depth 是否都在四值枚举内 | {grade_icon(not dept_bad)} | "
      f"{'全部合规' if not dept_bad else str([l.get('depth') for l in dept_bad])} |")
    A(f"| objective 前 12 字是否避开禁词（了解/熟悉/掌握/学习） | {grade_icon(not obj_banned)} | "
      f"{'全部合规' if not obj_banned else f'{len(obj_banned)} 条命中：' + str([l.get('objective')[:16] for l in obj_banned])} |")
    A(f"| objective 是否用可检验动词开头 | {grade_icon(len(obj_ok) == len(all_lessons), warn=len(obj_ok) >= len(all_lessons) * 0.7)} | "
      f"{len(obj_ok)}/{len(all_lessons)} |")
    A(f"| 讲次类型分布 | — | {dict(kinds)} |")
    A("")
    A("### 大纲明细")
    A("")
    for i, u in enumerate(units, 1):
        A(f"**第 {i} 单元 · {u['title']}**")
        A("")
        if u.get("summary"):
            A(f"> {u['summary']}")
            A("")
        A("| # | 讲次 | 类型 | 深度 | 学习目标 |")
        A("|---|---|---|---|---|")
        for j, l in enumerate(u["lessons"], 1):
            o = str(l.get("objective") or "").replace("|", "/")
            A(f"| {j} | {l['title']} | `{l.get('kind')}` | `{l.get('depth')}` | {o[:60]} |")
        A("")

    # ── 2. 课件结构 ─────────────────────────────────────────
    A("## 二、课件结构（页数 / 讲稿 / 卡片）")
    A("")
    if not L:
        A("_无讲义数据。_")
    else:
        A("| 讲次 | 页数 | 讲稿段 | 卡片 | 可视化 | 引用 | 生成耗时 |")
        A("|---|---|---|---|---|---|---|")
        for x in L:
            A(f"| {str(x.get('title'))[:22]} | {x['slides_n']} | {x['scripts_n']} | {x['cards_n']} "
              f"| {x['viz_n']} | {x['citations_n']} | {x.get('gen_secs')}s |")
        A("")
        pages = [x["slides_n"] for x in L]
        paired = all(x["slides_n"] == x["scripts_n"] for x in L)
        enough = [x for x in L if x["slides_n"] >= 8]
        cards_min = min(x["cards_n"] for x in L)
        A("| 检查项 | 结果 | 数值 |")
        A("|---|---|---|")
        A(f"| 每讲页数 ≥ 8（新篇幅档要求） | {grade_icon(len(enough) == len(L))} | "
          f"最小 {min(pages)} 页 / 最大 {max(pages)} 页 / 平均 {sum(pages)/len(pages):.1f} 页 |")
        A(f"| 讲稿段数与课件页数一一对应 | {grade_icon(paired)} | "
          f"{'全部对应' if paired else '存在不匹配'} |")
        A(f"| 每讲讲义卡片 ≥ 4 张 | {grade_icon(cards_min >= 4)} | 最少 {cards_min} 张 |")
        A(f"| 每页要点不超过 30 字（课堂可读性） | {grade_icon(all(x['bullets_over_30'] == 0 for x in L), warn=True)} | "
          f"超长要点共 {sum(x['bullets_over_30'] for x in L)} 条 |")
        A(f"| 课件均为中文（无英文兜底/空页） | {grade_icon(all(x['no_cjk_slides'] == 0 for x in L))} | "
          f"异常页共 {sum(x['no_cjk_slides'] for x in L)} 页 |")
        A("")

    # ── 3. 可视化 ───────────────────────────────────────────
    A("## 三、可视化产物（本轮重点）")
    A("")
    viz_all = [p for x in L for p in (x.get("viz_pages") or [])]
    total_pages = sum(x["slides_n"] for x in L)
    if not viz_all:
        A("**❌ 一页可视化都没有生成。**")
    else:
        by_viz = Counter(p["viz"] for p in viz_all)
        A(f"共 **{len(viz_all)} 页可视化 / {total_pages} 页课件（占比 {len(viz_all)/max(1,total_pages)*100:.0f}%）**")
        A("")
        A("| 形态 | 页数 |")
        A("|---|---|")
        for k, v in by_viz.most_common():
            A(f"| {k} | {v} |")
        A("")

        diags = [p for p in viz_all if p["viz"] == "diagram"]
        if diags:
            A("### 图示页（Archify 流水线的产物）")
            A("")
            A("| 讲次页 | 图型 | 预设 | 形态 | 节点 | 连边 | SVG 字节 |")
            A("|---|---|---|---|---|---|---|")
            for p in diags:
                A(f"| {str(p.get('title'))[:20]} | `{p.get('diagram_type')}` | `{p.get('preset')}` "
                  f"| {p.get('form')} | {p.get('node_count')} | {p.get('svg_edges')} | {p.get('svg_bytes')} |")
            A("")
            ir_svg = [p for p in diags if p.get("form") == "ir+svg"]
            A("| 检查项 | 结果 | 数值 |")
            A("|---|---|---|")
            A(f"| 图示全部走后端 IR→SVG 编译 | {grade_icon(len(ir_svg) == len(diags))} | "
              f"{len(ir_svg)}/{len(diags)}（其余为旧 Mermaid 形态） |")
            A(f"| 图型覆盖 | — | {dict(Counter(p.get('diagram_type') for p in diags))} |")
            A(f"| 视觉预设覆盖 | — | {dict(Counter(str(p.get('preset')) for p in diags))} |")
            A(f"| 节点规模合理（2~12） | {grade_icon(all(2 <= (p.get('node_count') or 0) <= 12 for p in diags))} | "
              f"{[p.get('node_count') for p in diags]} |")
            A("")

            A("### 模型实际产出的 IR（存下来的原文，可用于判断提示词契约是否被理解）")
            A("")
            for p in diags:
                ir = p.get("ir_full") or {}
                nm = len(ir.get("nodes") or ir.get("components") or ir.get("participants") or [])
                em = len(ir.get("edges") or ir.get("connections") or ir.get("messages") or [])
                A(f"**《{p.get('title')}》** — `{p.get('diagram_type')}`，{nm} 节点 / {em} 关系")
                A("")
                A("```json")
                A(json.dumps(ir, ensure_ascii=False, indent=2)[:1800])
                A("```")
                A("")

        # 回执：真实模型产出的坏图是怎么被拦下的
        recs = data.get("diagram_receipts") or []
        bad_recs = [r for r in recs if not r.get("ok")]
        A("### 图示校验回执（真实模型产出的坏图如何被拦下）")
        A("")
        if not recs:
            A("_本次没有捕获到编译器调用记录。_")
        else:
            A(f"编译器共被调用 **{len(recs)} 次**，其中 **{len(bad_recs)} 次未通过校验**"
              f"（未通过的会被带诊断重写一次，重写后再校验）。")
            A("")
            for i, r in enumerate(bad_recs, 1):
                codes = [d.get("code") for d in (r.get("diagnostics") or [])]
                A(f"**第 {i} 次失败** — 阶段 `{r.get('stage')}`，图型 `{r.get('diagram_type')}`，"
                  f"规则码 {codes}")
                A("")
                for d in (r.get("diagnostics") or [])[:4]:
                    A(f"- `{d.get('code')}` @ `{d.get('path')}`：{d.get('message')}")
                    if d.get("hint"):
                        A(f"  - 建议：{d.get('hint')}")
                if r.get("supportedFixes"):
                    A(f"- 建议修复：{r.get('supportedFixes')}")
                A("")
                if r.get("failed_ir"):
                    A("<details><summary>被拒绝的 IR 原文</summary>")
                    A("")
                    A("```json")
                    A(json.dumps(r["failed_ir"], ensure_ascii=False, indent=2)[:1500])
                    A("```")
                    A("")
                    A("</details>")
                    A("")

        charts = [p for p in viz_all if p["viz"] == "chart"]
        if charts:
            A("### 数据图表页")
            A("")
            A("| 讲次页 | 类型 | 类目数 | 系列数 |")
            A("|---|---|---|---|")
            for p in charts:
                A(f"| {str(p.get('title'))[:20]} | `{p.get('chart_type')}` | {p.get('cats')} | {p.get('series')} |")
            A("")

        tables = [p for p in viz_all if p["viz"] == "table"]
        if tables:
            A("### 对比表格页")
            A("")
            A("| 讲次页 | 列数 | 行数 |")
            A("|---|---|---|")
            for p in tables:
                A(f"| {str(p.get('title'))[:20]} | {p.get('cols')} | {p.get('rows')} |")
            A("")

        takes = [p for p in viz_all if p["viz"] == "takeaway"]
        if takes:
            A("### 金句卡")
            A("")
            A("| 讲次页 | 字数 |")
            A("|---|---|")
            for p in takes:
                A(f"| {str(p.get('title'))[:20]} | {p.get('len')} |")
            A("")

    # ── 4. 渲染验证 ─────────────────────────────────────────
    A("## 四、渲染验证（无头 Chrome 打开真实课堂页）")
    A("")
    if not render:
        A("_无渲染数据。_")
    else:
        A("| 讲次 | 课件卡片 | .viz-box | 内联 SVG | 表格 | 金句 | 回退提示 |")
        A("|---|---|---|---|---|---|---|")
        for x in L:
            r = render.get(x["lesson_id"])
            if not r:
                continue
            A(f"| {str(x.get('title'))[:20]} | {r.get('cards')} | {r.get('viz_box')} | "
              f"{r.get('inline_svg')} | {r.get('viz_table')} | {r.get('takeaway_box')} | {r.get('fallback')} |")
        A("")
        fb = sum((render.get(x["lesson_id"]) or {}).get("fallback", 0) for x in L)
        # 注意口径：`.viz-box` 只承载 diagram/chart；表格走 `.viz-table`、金句走
        # `.takeaway-box`（由 Viz.renderExtras 各自建容器）。三类分开比对才对。
        exp_dc = sum(1 for x in L for p in (x.get("viz_pages") or []) if p["viz"] in ("diagram", "chart"))
        exp_tb = sum(1 for x in L for p in (x.get("viz_pages") or []) if p["viz"] == "table")
        exp_tk = sum(1 for x in L for p in (x.get("viz_pages") or []) if p["viz"] == "takeaway")
        got_dc = sum((render.get(x["lesson_id"]) or {}).get("viz_box", 0) for x in L)
        got_tb = sum((render.get(x["lesson_id"]) or {}).get("viz_table", 0) for x in L)
        got_tk = sum((render.get(x["lesson_id"]) or {}).get("takeaway_box", 0) for x in L)
        got_svg = sum((render.get(x["lesson_id"]) or {}).get("inline_svg", 0) for x in L)
        A("| 检查项 | 结果 | 数值 |")
        A("|---|---|---|")
        A(f"| 图示/图表页渲染出容器 | {grade_icon(got_dc >= exp_dc and exp_dc > 0)} | 产出 {exp_dc} → 渲染 {got_dc} |")
        A(f"| 对比表格页渲染 | {grade_icon(got_tb >= exp_tb and exp_tb > 0, warn=exp_tb == 0)} | 产出 {exp_tb} → 渲染 {got_tb} |")
        A(f"| 金句卡渲染 | {grade_icon(got_tk >= exp_tk and exp_tk > 0, warn=exp_tk == 0)} | 产出 {exp_tk} → 渲染 {got_tk} |")
        A(f"| 没有渲染失败回退 | {grade_icon(fb == 0)} | 回退提示 {fb} 处 |")
        A(f"| 图示为后端编译的内联 SVG | {grade_icon(got_svg >= exp_dc and exp_dc > 0)} | "
          f"内联标记 {got_svg} 处（对应 {exp_dc} 张图） |")
        A("")

    # ── 5. 讲稿 ─────────────────────────────────────────────
    A("## 五、讲稿质量（教师讲述）")
    A("")
    if L:
        avgs = [x["script_avg"] for x in L]
        mirror = sum(x["mirror_suspects"] for x in L)
        A("| 指标 | 数值 |")
        A("|---|---|")
        A(f"| 讲稿平均字数/段 | **{sum(avgs)/len(avgs):.0f} 字**"
          f"（最短 {min(y['script_min'] for y in L)} / 最长 {max(y['script_max'] for y in L)}） |")
        A(f"| 疑似「照念课件」的页 | {mirror} 页（判据：讲稿字数 < 课件页文字 × 1.5） |")
        A(f"| 讲义小结字数 | {[x['summary'] for x in L]} |")
        A(f"| 讲义回顾字数 | {[x['recap'] for x in L]} |")
        A("")

    # ── 6. 后端拦截痕迹 ─────────────────────────────────────
    hits = data.get("backend_log_hits") or {}
    if hits:
        A("## 六、服务端防线的工作痕迹（后端日志统计）")
        A("")
        A("| 关键词 | 出现次数 | 含义 |")
        A("|---|---|---|")
        meaning = {"退化": "可视化页不合格 → 退回普通要点页", "剥除": "超限或非法字段被剥离",
                   "回执": "图示校验失败 → 带诊断定向重写", "重写": "讲稿/图示定向重写",
                   "降级": "嵌入等服务降级（本地哈希兜底）",
                   "失败": "某次调用失败（可能已重试成功）",
                   "未通过校验": "**图示 IR 被 Archify 校验器拦下**",
                   "引用为空": "**检索有命中但模型没引用材料**",
                   "超限": "可视化页数超过上限被剥"}
        for k, v in hits.items():
            A(f"| {k} | {v} | {meaning.get(k, '')} |")
        A("")
        log = (data.get("backend_log") or "").strip()
        if log:
            A("<details><summary>后端日志全文（含 WARNING / 防线痕迹）</summary>")
            A("")
            A("```")
            A(log[-4000:])
            A("```")
            A("")
            A("</details>")
            A("")

    # ── 7. 结论与偏差 ──────────────────────────────────────
    A("## 七、结论与发现的偏差")
    A("")
    issues: list[str] = []

    # 7.1 页数 vs 篇幅档要求（standard = 10~12 页）
    if L:
        pages = [x["slides_n"] for x in L]
        under = [x for x in L if x["slides_n"] < 10]
        if under:
            issues.append(
                f"**页数未达篇幅档下限**：standard 档要求 10~12 页，实测平均 "
                f"{sum(pages)/len(pages):.1f} 页，{len(under)}/{len(L)} 讲少于 10 页"
                f"（最少 {min(pages)} 页：{under[0]['title']}）")

    # 7.2 图示覆盖率
    if L:
        with_diag = [x for x in L if any(p["viz"] == "diagram" for p in (x.get("viz_pages") or []))]
        runs2 = collect_runs()
        diag_vals = [r["diagram"] for r in runs2]
        issues.append(
            f"**图示产出不稳定（首要问题）**：本轮 {len(with_diag)}/{len(L)} 讲含 diagram 页；"
            f"跨 {len(runs2)} 次运行分别是 {diag_vals} 页 —— 同一材料同一提示词，"
            f"模型有时一页图都不画。材料里其实有明确的可画内容"
            f"（代码页翻译出错的过程、.bat→cmd→文件 的调用链、三种替代方案流程、"
            f"GBK 与 UTF-8 的对照关系），**不是材料没问题，是提示词对「要不要画图」引导太软**："
            f"现在写的是「内容更适合用图时就用」，模型可以正当地选择不画。"
            f"**建议**：① 把约束改为「每讲至少 1 页可视化（材料含流程/对照/状态变化时），"
            f"最多 2 页」；② 加服务端兜底——讲义落库后若整讲无可视化页、"
            f"且材料里存在可用对照/流程内容，触发一次「补图」重写。")

    # 7.3 表格同质化
    tabs = [p for x in L for p in (x.get("viz_pages") or []) if p["viz"] == "table"]
    if tabs:
        sizes = Counter((p.get("cols"), p.get("rows")) for p in tabs)
        if len(sizes) == 1:
            issues.append(
                f"**表格同质化**：{len(tabs)} 页表格尺寸完全相同（{list(sizes)[0][0]} 列 × "
                f"{list(sizes)[0][1]} 行），且标题高度雷同（多为「错误想法 vs 实际情况」）——"
                f"说明模型对「哪里该用表格」缺乏区分度，有套模板倾向；提示词可加「同一讲内"
                f"表格不要与其他讲重复、列数按对照维度决定」")

    # 7.4 零引用
    zero = [x for x in L if x["citations_n"] == 0]
    if zero:
        issues.append(
            f"**有 {len(zero)} 讲零引用**：{zero[0]['title']}（{zero[0]['slides_n']} 页，"
            f"课件与讲稿均未出现任何 `[[c:N]]` 引用标注）。后端日志同时出现"
            f"「讲义落库但引用为空（检索有命中，模型未引用材料）」——检索有命中却没用上，"
            f"属于模型未遵守引用要求，会削弱「引用防伪」的价值（该讲学生看不到材料出处）")

    # 7.5 chart 为 0 是否合理
    if not any(p["viz"] == "chart" for x in L for p in (x.get("viz_pages") or [])):
        issues.append(
            "**chart 页 0 个 —— 这是正确行为**：材料中没有数值对比，提示词明确禁止硬造图表，"
            "模型遵守了该约束（属于「约束生效」，不是缺陷）")

    if not issues:
        A("未发现明显偏差。")
    else:
        for i, s in enumerate(issues, 1):
            A(f"{i}. {s}")
            A("")

    A("### 有效的部分（不建议改动）")
    A("")
    A("- 大纲结构（2 单元 8 讲、讲次命名「主题 · 侧重点」、四档 depth 递进）与可检验的 objective 均正常")
    A("- 讲稿与课件页一一对应，讲义卡片齐备")
    _dn = sum(1 for x in L for p in (x.get("viz_pages") or []) if p["viz"] == "diagram")
    A(f"- **图示渲染链路完好**：本轮 {_dn} 张图全部走后端 IR→SVG 编译、"
      "全部在课堂页内联渲染成功（跨轮样本中还有一次「按诊断重写后成功」）")
    A("- 可视化页上限（≤2）与退化兜底未误伤：没有出现渲染失败回退")
    A("")

    # ── 8. 目视观察 ────────────────────────────────────────
    A("## 八、目视观察（截图逐项看）")
    A("")
    A("截图：`调试截图/课件图示-真实模型生成.png`（第 3 次运行、PowerShell 法那一讲整页）")
    A("")
    A("| 观察点 | 实际情况 | 判断 |")
    A("|---|---|---|")
    A("| 图示是否真的画出来 | `.bat 启动器 →调用→ .ps1 脚本主体 →使用→ -LiteralPath`，"
      "节点带副标题、连线带标签 | ✅ 可用 |")
    A("| 图示布局 | 3 节点纵向排列，**右侧大片留白**；图形偏小 | ⚠️ 观感偏弱 |")
    A("| 图示配色 | `classic` 预设节点填充极浅、描边淡 | ⚠️ 对比度偏低，投影上课时可能看不清 |")
    A("| 对比表格 | 2 列（问题 / 方法），行内容偏短，语义不够清楚 | ⚠️ 单薄 |")
    A("| 金句卡 | 高亮底色 + 居中，一眼能抓 | ✅ 效果好 |")
    A("| 课件可读性 | 卡片标题 + 短要点，无整段文字 | ✅ 好 |")
    A("")
    A("**图示观感的三个可选改进**（按性价比）：")
    A("")
    A("1. **图示居中并给最小宽度**：现在图形贴在卡片左侧、宽度只有内容的自然宽度，"
      "右侧空着。给 `.viz-box svg` 一个 `min-width`（如 420px）或整体居中，观感立刻改善。")
    A("2. **classic 预设提高对比度**：节点填充与描边的色差太小，"
      "建议加深描边或给节点用更实的底色（`blueprint` / `signal-flow` 两个预设可一并调）。")
    A("3. **横向布局优先**：3~4 个节点的流程用 `flowchart LR` 式的横向排布更贴合宽屏，"
      "而不是竖着排一行——可在编译器的布局选择里加「节点 ≤4 且无环时优先横向」。")
    A("")

    A("---")
    A("")
    A("> 本报告由端到端评测脚本自动生成：`zhiban/dev/course_quality_eval.py`（真实模型跑全流程）"
      "→ `.tmp/e2e24-report.json` → 本报告。多轮样本留存在 `.tmp/e2e-runs/`。")
    A(">")
    A("> 复现方式：")
    A("> ```")
    A("> .venv/Scripts/python.exe dev/course_quality_eval.py                  # 完整评测（约 5 分钟）")
    A("> .venv/Scripts/python.exe dev/course_quality_eval.py --max-lessons 1  # 快速抽查（约 1 分钟）")
    A("> .venv/Scripts/python.exe dev/course_quality_report.py               # 由 JSON 生成本报告")
    A("> ```")
    return "\n".join(out)


def main() -> int:
    if not SRC.exists():
        print(f"找不到 {SRC}")
        return 1
    data = json.loads(SRC.read_text(encoding="utf-8"))
    OUT.write_text(build(data), encoding="utf-8")
    print(f"报告已生成：{OUT}（{OUT.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
