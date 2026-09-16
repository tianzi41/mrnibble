# -*- coding: utf-8 -*-
"""架构化图示（Archify 式 IR → 确定性编译）单元套件。

不依赖后端进程：直接调 ``backend.services.diagram``，覆盖
① 五种图型都能编译出 SVG；② 每条校验规则码都有反例、回执结构完整；
③ 同一 IR 编译结果**逐字节确定**；④ **提示词契约与校验器同源**（用户明确要求的一致性）。

跑法：``PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe dev/diagram_ir_check.py``
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from backend.services import diagram as D  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  | {detail}" if detail and not cond else ""))


# ── 样例 IR（五种图型各一份，内容取自真实的编码/批处理教学主题）──

WF = {"schema_version": 1, "diagram_type": "workflow", "meta": {"title": "chcp 设置流程"},
      "lanes": [{"id": "l1", "label": "准备"}, {"id": "l2", "label": "执行"}],
      "nodes": [{"id": "open", "type": "frontend", "label": "打开命令行", "lane": "l1"},
                {"id": "chcp", "type": "backend", "label": "运行 chcp", "sublabel": "查看代码页", "lane": "l2"},
                {"id": "set", "type": "backend", "label": "切换代码页", "lane": "l2"}],
      "edges": [{"id": "e1", "from": "open", "to": "chcp", "label": "输入命令", "variant": "emphasis"},
                {"id": "e2", "from": "chcp", "to": "set", "label": "确认编号"}]}

SEQ = {"schema_version": 1, "diagram_type": "sequence", "meta": {"title": "读取文件时序"},
       "participants": [{"id": "app", "type": "frontend", "label": "程序"},
                        {"id": "os", "type": "backend", "label": "系统"}],
       "messages": [{"id": "m1", "from": "app", "to": "os", "label": "请求字节"},
                    {"id": "m2", "from": "os", "to": "app", "label": "返回内容",
                     "kind": "return", "variant": "dashed"}]}

DF = {"schema_version": 1, "diagram_type": "dataflow", "meta": {"title": "文本入库流程"},
      "stages": [{"id": "s1", "label": "采集"}, {"id": "s2", "label": "处理"}],
      "nodes": [{"id": "raw", "type": "external", "label": "原始文本", "stage": "s1"},
                {"id": "cut", "type": "backend", "label": "切片", "stage": "s2"}],
      "flows": [{"id": "f1", "from": "raw", "to": "cut", "label": "送入"}]}

LC = {"schema_version": 1, "diagram_type": "lifecycle", "meta": {"title": "编码检测状态"},
      "states": [{"id": "init", "type": "start", "label": "开始"},
                 {"id": "check", "type": "decision", "label": "有 BOM?"},
                 {"id": "ok", "type": "success", "label": "正常读取"},
                 {"id": "bad", "type": "failure", "label": "乱码"}],
      "transitions": [{"id": "t1", "from": "init", "to": "check"},
                      {"id": "t2", "from": "check", "to": "ok", "label": "无"},
                      {"id": "t3", "from": "check", "to": "bad", "label": "有", "variant": "security"}]}

ARCH = {"schema_version": 1, "diagram_type": "architecture",
        "meta": {"title": "批处理运行环境", "preset": "blueprint"},
        "components": [{"id": "cli", "type": "external", "label": "用户终端"},
                       {"id": "cmd", "type": "backend", "label": "cmd.exe", "sublabel": "解释器"},
                       {"id": "fs", "type": "database", "label": "文件系统"}],
        "boundaries": [{"kind": "region", "label": "本机", "wraps": ["cmd", "fs"]}],
        "connections": [{"id": "c1", "from": "cli", "to": "cmd", "label": "输入命令"},
                        {"id": "c2", "from": "cmd", "to": "fs", "label": "读写"}]}

SAMPLES = {"workflow": WF, "sequence": SEQ, "dataflow": DF, "lifecycle": LC, "architecture": ARCH}


def main() -> int:
    print("[1] 五种图型都能编译出 SVG")
    for name, ir in SAMPLES.items():
        svg, rec = D.compile_ir(ir)
        check(f"1.{name} 编译成功", rec["ok"] and bool(svg),
              str(rec.get("diagnostics"))[:160])
    # 结构细节
    svg_wf, _ = D.compile_ir(WF)
    check("1.a workflow 含全部节点与关系",
          svg_wf.count('class="zf-node"') == 3 and svg_wf.count('class="zf-edge"') == 2,
          f"nodes={svg_wf.count('zf-node')} edges={svg_wf.count('zf-edge')}")
    svg_seq, _ = D.compile_ir(SEQ)
    check("1.b sequence 画出参与者生命线",
          svg_seq.count("zf-lifeline") == 2, str(svg_seq.count("zf-lifeline")))
    svg_arch, _ = D.compile_ir(ARCH)
    check("1.c architecture 画出边界框并带上预设",
          "zf-boundary" in svg_arch and 'data-preset="blueprint"' in svg_arch)
    check("1.d 边标签带上白底衬（避免压在线上看不清）",
          "paint-order=\"stroke\"" in svg_wf)
    check("1.e SVG 是自包含的（带 viewBox、无外部引用）",
          "viewBox=" in svg_wf and "http://" not in svg_wf.replace("http://www.w3.org/2000/svg", ""))

    print("\n[2] 校验规则码：每条反例都应被拦下，且回执可执行")
    BAD = {
        "schema/unknown-field": {**ARCH, "components": [{**ARCH["components"][0], "pos": [1, 2]},
                                                        *ARCH["components"][1:]]},
        "schema/pattern": {**WF, "nodes": [{**WF["nodes"][0], "id": "1bad"}, *WF["nodes"][1:]]},
        "schema/enum": {**WF, "nodes": [{**WF["nodes"][0], "type": "gadget"}, *WF["nodes"][1:]]},
        "schema/required": {"schema_version": 1, "diagram_type": "workflow", "nodes": [], "edges": []},
        "schema/limit": {**WF, "nodes": [{**WF["nodes"][0], "label": "一个远超十四个字的超长节点标签"}]
                                   + WF["nodes"][1:]},
        "graph/duplicate-id": {**WF, "nodes": [*WF["nodes"], {"id": "open", "label": "重复"}]},
        "graph/dangling-ref": {**WF, "edges": [*WF["edges"], {"id": "e9", "from": "open", "to": "ghost"}]},
        "graph/self-loop": {**WF, "edges": [*WF["edges"], {"id": "e8", "from": "open", "to": "open"}]},
        "graph/orphan-node": {**WF, "nodes": [*WF["nodes"], {"id": "lonely", "label": "孤立", "lane": "l1"}]},
        "graph/duplicate-relation-id": {**WF, "edges": [*WF["edges"],
                                                        {"id": "e1", "from": "chcp", "to": "open"}]},
        "view/unknown-focus": {**WF, "meta": {**WF["meta"],
                                              "views": [{"id": "v1", "label": "x", "focus": ["ghost"]}]}},
    }
    for code, ir in BAD.items():
        svg, rec = D.compile_ir(ir)
        codes = [d["code"] for d in rec["diagnostics"]]
        ok = (not rec["ok"]) and svg is None and code in codes
        check(f"2.{code} 被拦下且有修复建议",
              ok and bool(rec["supportedFixes"])
              and all({"code", "path", "message"} <= set(d) for d in rec["diagnostics"]),
              f"codes={codes[:3]} fixes={len(rec['supportedFixes'])}")
    # 诊断要能定位到具体对象（Archify 的 (id/label: "...") 风格）
    _, rec_dup = D.compile_ir(BAD["graph/dangling-ref"])
    diag = rec_dup["diagnostics"][0]
    check("2.a 诊断带实例路径与规则码", diag["path"].startswith("/edges/") and diag["code"],
          str(diag)[:160])
    check("2.b 悬空引用带 known_ids 证据",
          bool(diag.get("evidence", {}).get("known_ids")), str(diag.get("evidence")))
    check("2.c 回执结构与 Archify 同构（stage/diagnostics/supportedFixes）",
          {"ok", "stage", "diagram_type", "diagnostics", "supportedFixes"} <= set(rec_dup))
    check("2.d 阶段可区分（形状错 vs 图结构错）",
          D.compile_ir(BAD["schema/pattern"])[1]["stage"] == "schema"
          and D.compile_ir(BAD["graph/self-loop"])[1]["stage"] == "graph")

    print("\n[3] 确定性：同一 IR 必须编译出逐字节相同的 SVG")
    a, _ = D.compile_ir(ARCH)
    b, _ = D.compile_ir(ARCH)
    check("3.1 二次编译结果一致", a == b)
    c, _ = D.compile_ir({**ARCH, "meta": {**ARCH["meta"], "preset": "classic"}})
    check("3.2 换预设会改变视觉（但仍是合法 SVG）",
          c != a and "zf-node" in c and 'data-preset="classic"' in c)

    print("\n[4] 提示词契约与校验器同源（防「改了校验器忘了改提示词」）")
    spec = D.prompt_spec()
    check("4.1 契约文本列出全部图型",
          all(t in spec for t in D.DIAGRAM_TYPES), str(D.DIAGRAM_TYPES))
    check("4.2 契约文本列出全部节点 type 与 state type",
          all(t in spec for t in D.NODE_TYPES) and all(t in spec for t in D.STATE_TYPES))
    check("4.3 契约文本列出全部视觉预设", all(t in spec for t in D.PRESETS))
    check("4.4 契约文本的数字上限与校验器常量一致",
          all(str(D.LIMITS[k]) in spec for k in ("nodes", "relations", "label", "sublabel", "title")),
          str(D.LIMITS))
    check("4.5 契约明确禁止 SVG/HTML/坐标",
          "绝不写 SVG" in spec and "不要给任何坐标" in spec)
    check("4.6 契约要求实体来自材料", "必须是材料里有的" in spec)

    src = (ROOT / "src" / "backend" / "services" / "courses.py").read_text(encoding="utf-8")
    check("4.7 _LECTURE_PROMPT 用占位符引用契约（不重复维护文案）", "__DIAGRAM_SPEC__" in src)
    check("4.8 讲义生成时真的注入了契约",
          'replace("__DIAGRAM_SPEC__", diagram_mod.prompt_spec())' in src)
    check("4.9 回执重写提示词存在且要求「只修被点名的对象」",
          "_DIAGRAM_REWRITE_PROMPT" in src and "只修被诊断点名的对象" in src)
    check("4.10 编译失败路径是「重写一次 → 退化要点页」",
          "_rewrite_diagram" in src and "图示重写后仍不合格，已退化为要点页" in src)

    print("\n[5] 编译入口只接受 IR；旧 Mermaid 形态由调用方分流（不在这里被判坏）")
    legacy = {"lang": "mermaid", "code": "flowchart TD\n  A[读取] --> B[解析]"}
    svg, rec = D.compile_ir(legacy)
    codes = [d["code"] for d in rec["diagnostics"]]
    check("5.1 非 IR 输入不会崩，且指出 diagram_type 非法与多余字段",
          svg is None and rec["ok"] is False and rec["stage"] == "schema"
          and "schema/enum" in codes and "schema/unknown-field" in codes,
          str(codes[:3]))
    check("5.2 诊断是可执行的（含 hint）", bool(rec["supportedFixes"]))
    courses_src = (ROOT / "src" / "backend" / "services" / "courses.py").read_text(encoding="utf-8")
    check("5.3 courses 侧保留旧 Mermaid 判定（向后兼容旧讲义数据）",
          "_valid_mermaid" in courses_src
          and "_valid_diagram" in courses_src
          and "return cls._valid_mermaid(diagram)" in courses_src)

    # ── [6] 补图兜底的前置判定 + 提示词硬约束（与校验器同源）──
    print("\n[6] 补图兜底判定与提示词硬约束")
    sys.path.insert(0, str(ROOT / "src"))
    from backend.services.courses import (          # noqa: PLC0415
        _DEPTH_HINT, _DESC_FILL_PROMPT, _DESC_SPEC, _LECTURE_PROMPT,
        _OUTLINE_PROMPT, _material_looks_visual,
    )

    positive = ("第一步先判断文件编码，第二步再解析内容，顺序不能颠倒；"
                "两种编码方式在处理中文路径时差异明显，层级结构也不一样。") * 3
    negative = ("写作最重要的是保持耐心与好奇心。多读多写，时间久了自然会有进步，"
                "不要指望一蹴而就，也别总和别人比较。") * 3
    check("6.1 材料含流程/对照结构 → 判定可补图", _material_looks_visual(positive) is True)
    check("6.2 纯叙述材料 → 不补图（不硬造图表）", _material_looks_visual(negative) is False)
    check("6.3 材料过短 → 不补图",
          _material_looks_visual("步骤 流程 顺序 对照 区别 结构") is False)
    check("6.4 讲义提示词含图示硬要求",
          "至少安排 1 页 diagram" in _LECTURE_PROMPT
          and "**硬要求**" in _LECTURE_PROMPT
          and "若出现「两/多种方案或做法的逐项对照」→ 必须 ≥1 页 table" in _LECTURE_PROMPT)
    check("6.5 篇幅档页数下限已收紧且写明「不合格」",
          "少于 10 页即不合格" in _DEPTH_HINT["brief"]
          and "少于 12 页即不合格" in _DEPTH_HINT["standard"]
          and "少于 14 页即不合格" in _DEPTH_HINT["detailed"])
    check("6.6 大纲提示词把讲次数交给内容体量决定",
          "__LESSONS_RULE__" in _OUTLINE_PROMPT
          and "每个单元 2~4 个讲次" not in _OUTLINE_PROMPT
          and "练习也计入讲次数" in _OUTLINE_PROMPT)

    # 6.7 带环 workflow：校验器不拒绝环（环是合法语义，如「重试」），
    #     但分层算法曾是「不断放宽层号」的写法，在环上层号无限增长 → 死循环。
    #     （实测踩到：单 lane 工作流被路由进拓扑分层后挂死，进程无输出直接被杀。）
    cyclic_ir = {
        "schema_version": 1, "diagram_type": "workflow",
        "meta": {"title": "重试循环", "preset": "classic"},
        "nodes": [{"id": "run", "type": "backend", "label": "执行命令"},
                  {"id": "chk", "type": "frontend", "label": "检查输出"},
                  {"id": "retry", "type": "backend", "label": "重试"}],
        "edges": [{"id": "e1", "from": "run", "to": "chk"},
                  {"id": "e2", "from": "chk", "to": "retry", "variant": "dashed"},
                  {"id": "e3", "from": "retry", "to": "run", "variant": "dashed"}],
    }
    csvg, crec = D.compile_ir(cyclic_ir)
    check("6.7 带环 workflow 能编译（分层算法对环免疫，不再死循环）",
          bool(csvg) and crec["ok"] is True and "zf-node" in csvg,
          json.dumps(crec, ensure_ascii=False)[:160])
    csvg2, _ = D.compile_ir(cyclic_ir)
    check("6.8 带环图同样逐字节确定", csvg == csvg2)

    # 6.9/6.10 desc 字段规范的同源断言。
    # 背景：大纲提示词里的**整段 desc 规范**曾因编辑竞态被静默吃掉，只剩自查清单里
    # 两句提及 —— 模型完全不知道要输出 desc 字段。而测试走的是自带 desc 的 mock 桩，
    # 八轮回归全绿却没暴露。教训：提示词里的关键字段定义，必须有断言盯着。
    check("6.9 大纲提示词含 desc 规范占位与 desc 的 schema 示例",
          "__DESC_SPEC__" in _OUTLINE_PROMPT and '"desc":{"outcomes"' in _OUTLINE_PROMPT)
    check("6.10 desc 规范单一来源：常量字段齐全，且「补写 desc」提示词引用同一份",
          all(k in _DESC_SPEC for k in ("outcomes", "knowledge_points", "concepts",
                                        "operations", "transition", "exercise_focus",
                                        "expected_mistakes", "exercise_flow"))
          and "__DESC_SPEC__" in _DESC_FILL_PROMPT)

    # 6.11/6.12 编辑结构必须把 desc **原样带回**。
    # `confirm_outline` 按 (unit, ordinal) 序号位置复用讲次行 —— 前端提交时若丢掉 desc，
    # 被删讲次之后的每一讲都会继承前一讲的 desc（静默错位，会错误约束讲义内容）。
    web_src = (ROOT / "src" / "web" / "js" / "views" / "courses.js").read_text(encoding="utf-8")
    _ok_at = web_src.find('getElementById("o-ok")')     # 提交处理函数（不是 HTML 里的按钮定义）
    ok_seg = web_src[_ok_at:_ok_at + 2500] if _ok_at >= 0 else ""
    check("6.11 前端「确认结构」提交时带 desc（防编辑结构后 desc 错位）",
          "desc: l.desc || null" in ok_seg, f"提交段定位={_ok_at}")
    check("6.12 后端 confirm_outline 按「有无 desc 键」分支写/清 desc_json",
          'elif "desc" in lesson:' in courses_src
          and "desc_json=?" in courses_src)

    # 6.13/6.14 课型库同源（2026-09-16）：课型是「一份定义」——
    # 提示词里的结构推进顺序直接取它的 flow，前端经接口取同一份。三处各写一份必然漂移
    # （desc 规范整段丢失过一次，就是没有断言盯着的后果）。
    from backend.services.courses import (          # noqa: PLC0415
        _INTENT_TYPES, _intent_spec, intent_by_id, intent_catalog,
    )
    live = [t["id"] for t in intent_catalog()]
    reserved = [t["id"] for t in _INTENT_TYPES if not t["live"]]
    check("6.13 课型库：已上线 5 种 + 预留项只由 live 标记控制（上线无需改前端）",
          live == ["overview", "deep-read", "exam", "inquiry", "project"]
          and len(reserved) >= 3 and "recite" in reserved and "contrast" in reserved,
          f"live={live} reserved={reserved}")
    dr = intent_by_id("deep-read")
    spec = _intent_spec("deep-read", "exam", "学生初三")
    check("6.14 课型块直接取该课型的 flow（单一来源）+ 大纲提示词留了占位",
          bool(dr) and dr["flow"][0] in spec and dr["flow"][-1] in spec
          and "辅助课型" in spec and "学生初三" in spec
          and "__INTENT_SPEC__" in _OUTLINE_PROMPT
          and _intent_spec("", None) == "" and _intent_spec("recite", None) == "")

    print("\n" + "=" * 60)
    print(f"架构化图示套件：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
