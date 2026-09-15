"""导出「Archify 图示样例画廊」—— 把历次运行里编译好的图示拼成一个可浏览的 HTML。

为什么要这个工具：
  图示是**后端确定性编译**出来的 SVG，存在各次评测的隔离库（`.tmp/e2e24-*/data/zhiban.db`）
  与当前交付库（`dist*/知伴/data/zhiban.db`）里。想「实际看看长什么样」时，
  不用起后端、不用翻数据库，直接跑本脚本生成静态画廊即可（SVG 自包含，无外部依赖）。

内容：
  1. 每张编译好的图示：渲染效果 + 元信息（图型/预设/规模）+ **原始 IR**（模型写了什么）
  2. 同一份 IR 在 4 个视觉预设下的效果对照
  3. 被校验器**拦下并退回**的坏图（从 `receipts.jsonl` 取）+ 诊断规则码

用法：
    .venv/Scripts/python.exe dev/diagram_gallery.py
    .venv/Scripts/python.exe dev/diagram_gallery.py --out "自定义路径.html"
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))      # --recompile 需要 import backend.services.diagram
DEFAULT_OUT = ROOT.parent / "archify图示样例.html"

CSS = """
:root{--bg:#f6f7fb;--card:#fff;--border:#e5e7eb;--text:#1f2430;--muted:#6b7280;
      --primary:#4f46e5;--soft:#eef2ff;--bad:#b91c1c;--badsoft:#fef2f2}
*{box-sizing:border-box}
body{margin:0;padding:28px;background:var(--bg);color:var(--text);
     font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1{font-size:22px;margin:0 0 6px}
h2{font-size:17px;margin:34px 0 12px;padding-bottom:8px;border-bottom:2px solid var(--border)}
.lead{color:var(--muted);margin:0 0 4px}
.pipe{background:var(--card);border:1px solid var(--border);border-radius:10px;
      padding:12px 16px;margin:16px 0 8px;font-size:13px}
.pipe b{color:var(--primary)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:18px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;
      padding:14px 16px 10px;display:flex;flex-direction:column;gap:10px}
.card.bad{border-color:#fecaca;background:var(--badsoft)}
.card h3{margin:0;font-size:15px}
.tags{display:flex;flex-wrap:wrap;gap:6px}
.tag{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--soft);
     color:var(--primary);border:1px solid #c7d2fe}
.tag.gray{background:#f3f4f6;color:var(--muted);border-color:var(--border)}
.tag.red{background:#fee2e2;color:var(--bad);border-color:#fecaca}
.canvas{border:1px dashed var(--border);border-radius:8px;padding:14px;background:#fff;
        overflow:auto}
.canvas svg{max-width:100%;height:auto;display:block;margin:0 auto}
.meta{font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:12px}
details{font-size:12px}
summary{cursor:pointer;color:var(--primary)}
pre{background:#0f172a;color:#e2e8f0;padding:10px 12px;border-radius:8px;overflow:auto;
    max-height:340px;font-size:11.5px;line-height:1.55}
.note{font-size:12px;color:var(--muted);margin-top:6px}
.empty{color:var(--muted);font-style:italic}
"""

PIPE = (
    '模型只产 typed IR（结构化事实，无坐标/无样式） → '
    '<b>后端校验</b>（schema → graph → layout，共 11 条规则码） → '
    '<b>通过则确定性编译成 SVG</b>（同一 IR 逐字节一致） → '
    '未通过则<b>带回执定向重写一次</b> → 仍不合格<b>剥字段退化为要点页</b>'
)


def _card(title: str, sub: str, tags: list[str], svg: str, meta: list[str],
          ir: dict | None, extra: str = "") -> str:
    tag_html = "".join(f'<span class="tag{" gray" if t.startswith("@") else ""}">'
                       f'{html.escape(t.lstrip("@"))}</span>' for t in tags)
    ir_html = ""
    if ir is not None:
        ir_html = ("<details><summary>模型写的原始 IR（程序就是照着它画的）</summary>"
                   f"<pre>{html.escape(json.dumps(ir, ensure_ascii=False, indent=2))}</pre>"
                   "</details>")
    return f"""<div class="card">
  <h3>{html.escape(title)}</h3>
  <div class="tags">{tag_html}</div>
  <div class="canvas">{svg}</div>
  <div class="meta">{''.join(f'<span>{html.escape(m)}</span>' for m in meta)}</div>
  {ir_html}{extra}
  <div class="note">{html.escape(sub)}</div>
</div>"""


def collect(db: Path, label: str, recompile: bool = False) -> list[dict]:
    """从库里取出所有已编译的图示页。"""
    out: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        return out
    try:
        rows = conn.execute(
            "SELECT title, slides_json FROM course_lessons WHERE slides_json IS NOT NULL"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return out
    for lesson_title, raw in rows:
        try:
            slides = json.loads(raw or "[]")
        except Exception:  # noqa: BLE001
            continue
        for sl in slides:
            dg = (sl or {}).get("diagram") or {}
            if not isinstance(dg, dict):
                continue
            ir = dg.get("ir") or {}
            svg = dg.get("svg")
            # --recompile：用**当前编译器**重画历史 IR（验证布局改进用）；
            # 旧 Mermaid 形态没有 IR，保持原样跳过。
            if recompile and ir:
                from backend.services import diagram as _D  # noqa: PLC0415
                svg, _rec = _D.compile_ir(ir)
                if not svg:
                    continue
            if not svg:
                continue
            out.append({"source": label, "lesson": lesson_title,
                        "slide": sl.get("title") or "", "svg": svg,
                        "ir": ir, "type": dg.get("diagram_type") or "",
                        "preset": dg.get("preset") or "classic",
                        "bullets": sl.get("bullets") or []})
    conn.close()
    return out


def collect_rejected() -> list[dict]:
    """取出被校验器拦下的坏图（规则码 + 被拒 IR）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for f in sorted((ROOT / ".tmp").glob("e2e24-*/receipts.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            continue
        for ln in lines:
            try:
                rec = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("ok") or not rec.get("failed_ir"):
                continue
            key = json.dumps(rec.get("diagnostics"), ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            out.append(rec)
    return out


def build(out_path: Path, recompile: bool = False) -> int:
    items: list[dict] = []
    # 历次评测的隔离库（真实模型跑出来的产物）
    for d in sorted((ROOT / ".tmp").glob("e2e24-*/data/zhiban.db")):
        items += collect(d, d.parent.parent.name, recompile=recompile)
    # 当前交付库（用户自己的课）
    for d in sorted(ROOT.glob("dist*/知伴/data/zhiban.db")):
        items += collect(d, d.parent.parent.name + "（你的库）", recompile=recompile)

    rejected = collect_rejected()

    # 预设对照：拿第一张图的 IR，按 4 个预设各编译一次
    preset_demo = ""
    if items:
        sys.path.insert(0, str(ROOT / "src"))
        from backend.services import diagram as D  # noqa: PLC0415
        base_ir = items[0]["ir"]
        cards = []
        for preset in D.PRESETS:
            ir = json.loads(json.dumps(base_ir, ensure_ascii=False))
            ir.setdefault("meta", {})["preset"] = preset
            svg, rec = D.compile_ir(ir)
            if svg:
                cards.append(_card(f"预设：{preset}", "同一份 IR，只换 meta.preset",
                                   [f"@{rec.get('stage')}", "编译成功"], svg,
                                   [f"{len(svg)} 字节"], None))
        if cards:
            preset_demo = ('<h2>二、同一份 IR × 4 个视觉预设</h2>'
                           '<p class="lead">预设只影响配色与字体风格，几何布局由编译器决定，'
                           '模型无从干预。</p>'
                           f'<div class="grid">{"".join(cards)}</div>')

    # 正文
    cards_html = []
    for it in items:
        ir = it["ir"]
        nodes = len(ir.get("nodes") or ir.get("components") or ir.get("participants")
                    or ir.get("states") or [])
        rels = len(ir.get("edges") or ir.get("connections") or ir.get("messages")
                   or ir.get("transitions") or ir.get("flows") or [])
        cards_html.append(_card(
            it["slide"] or "（无标题）",
            f"来源：{it['source']} ／ 讲次：{it['lesson']}",
            [it["type"], f"@{it['preset']}"],
            it["svg"],
            [f"节点 {nodes}", f"关系 {rels}", f"SVG {len(it['svg'])} 字节"],
            ir,
        ))

    bad_html = []
    for rec in rejected[:6]:
        diags = rec.get("diagnostics") or []
        rows = "".join(
            f'<div>· <code>{html.escape(str(d.get("code")))}</code> '
            f'@ <code>{html.escape(str(d.get("path")))}</code>：'
            f'{html.escape(str(d.get("message") or ""))}</div>' for d in diags[:5])
        bad_html.append(_card(
            f"被拦下的坏图（阶段 {rec.get('stage')}，图型 {rec.get('diagram_type')}）",
            "校验器拒绝 → 服务端带着这些诊断让模型重写一次；重写仍不合格就退化为要点页，"
            "**不会**进入课件。",
            [f"@{rec.get('stage')}", f"{len(diags)} 条诊断"],
            f'<div style="font-size:12.5px;color:#7f1d1d">{rows}</div>',
            ["未生成 SVG"],
            rec.get("failed_ir"),
        ))

    parts = [
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<title>Archify 图示样例</title>", f"<style>{CSS}</style></head><body>",
        "<h1>Archify 图示样例（后端确定性编译的产物）</h1>",
        f'<p class="lead">共 {len(items)} 张编译好的图示，来自历次真实模型评测的运行记录；'
        f'另有 {len(rejected)} 类被校验器拦下的坏图样例。</p>',
        f'<div class="pipe">流水线：{PIPE}</div>',
        f"<h2>一、编译好的图示（{len(items)} 张）</h2>",
        f'<div class="grid">{"".join(cards_html)}</div>' if cards_html
        else '<p class="empty">没有找到编译后的图示。</p>',
        preset_demo,
    ]
    if bad_html:
        parts += [f"<h2>三、被校验器拦下的坏图（{len(rejected)} 类）</h2>",
                  '<p class="lead">这些 IR 没有被画出来。诊断里带稳定的规则码与出错路径，'
                  '模型据此只修被点名的对象。</p>',
                  f'<div class="grid">{"".join(bad_html)}</div>']
    parts.append("</body></html>")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"画廊已生成：{out_path}（{out_path.stat().st_size} 字节）")
    print(f"  编译好的图示 {len(items)} 张 | 被拦下的坏图 {len(rejected)} 类")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 Archify 图示样例画廊")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--recompile", action="store_true",
                    help="用当前编译器重画历史 IR（验证布局改进）")
    args = ap.parse_args()
    return build(Path(args.out), recompile=args.recompile)


if __name__ == "__main__":
    raise SystemExit(main())
