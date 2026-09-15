"""架构化图示编译器（Archify 机制的本地复刻）。

设计来源：`github.com/tt-a1i/archify`（MIT）—— 它的核心是
**「AI 只产 typed JSON IR → 确定性编译器画出 SVG/HTML，校验失败给机器可读的修复回执」**。

本模块把那套机制搬到知伴的课件图示上，但有**两处有意的偏离**（理由见
`架构化课件图示方案_v1.md` §3.3）：

1. **IR 不含坐标**（Archify 让 agent 给 ``pos``/``size``/``via``）。
   教学场景下确定性布局更可测、更稳，也避免模型算像素把图算崩；
   模型若写了坐标字段，会被 ``schema/unknown-field`` 明确拒绝。
2. **不做动效与自包含 HTML**，只出内联 SVG（直接进课件页）。

对外只暴露 :func:`compile_ir`：``ir -> (svg, receipt)``。
``receipt`` 与 Archify 同构：``{ok, stage, diagram_type, diagnostics[], supportedFixes[]}``，
其中 ``diagnostics`` 每条带**稳定规则码 + 实例路径 + 最近的 id/label 注解 + 证据**，
用于让模型「只修被点名的对象」而不是盲重试。
"""

from __future__ import annotations

import re
from html import escape as _esc
from typing import Any

# ── 契约常量（提示词与校验器共用同一份定义，避免「改了校验器忘了改提示词」）──

SCHEMA_VERSION = 1

DIAGRAM_TYPES = ("architecture", "workflow", "sequence", "dataflow", "lifecycle")
PRESETS = ("classic", "signal-flow", "blueprint", "editorial")

NODE_TYPES = ("frontend", "backend", "database", "cloud", "security", "messagebus", "external")
STATE_TYPES = ("start", "active", "waiting", "decision", "success", "failure", "neutral", "external")
VARIANTS = ("default", "emphasis", "security", "dashed")
SEQ_VARIANTS = ("default", "emphasis", "security", "dashed", "return")
BOUNDARY_KINDS = ("region", "security-group")
CARD_DOTS = ("emerald", "cyan", "rose", "amber", "violet")

LIMITS = {
    "nodes": 12,        # 节点/参与者/状态 ≤12
    "relations": 18,    # 关系 ≤18
    "label": 14,        # 标签 ≤14 字
    "sublabel": 12,     # 副标签 ≤12 字
    "title": 24,        # 图题 ≤24 字
    "boundaries": 2,    # 边界框 ≤2
    "views": 3,         # guided views ≤3
    "cards": 3,         # 摘要卡 ≤3
    "lanes": 4,
    "stages": 4,
}

_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")

# 每种图型的结构：节点集合名 / 关系集合名 / 允许出现的其余数组
_SPEC: dict[str, dict[str, Any]] = {
    "architecture": {
        "nodes": "components", "relations": "connections",
        "collections": ("components", "boundaries", "connections", "cards"),
        "required": ("components", "connections"),
        "group_by": "type",
    },
    "workflow": {
        "nodes": "nodes", "relations": "edges",
        "collections": ("lanes", "nodes", "edges", "cards"),
        "required": ("nodes", "edges"),
        "group_by": "lane",
    },
    "sequence": {
        "nodes": "participants", "relations": "messages",
        "collections": ("participants", "messages", "cards"),
        "required": ("participants", "messages"),
        "group_by": None,
    },
    "dataflow": {
        "nodes": "nodes", "relations": "flows",
        "collections": ("stages", "nodes", "flows", "cards"),
        "required": ("nodes", "flows"),
        "group_by": "stage",
    },
    "lifecycle": {
        "nodes": "states", "relations": "transitions",
        "collections": ("lanes", "states", "transitions", "cards"),
        "required": ("states", "transitions"),
        "group_by": "lane",
    },
}

# 各集合内允许的字段（``additionalProperties: false`` 的实现）
_FIELDS: dict[str, tuple[str, ...]] = {
    "components": ("id", "type", "label", "sublabel", "tag"),
    "nodes": ("id", "type", "label", "sublabel", "tag", "lane", "stage"),
    "participants": ("id", "type", "label", "sublabel", "tag"),
    "states": ("id", "type", "label", "sublabel", "tag", "lane"),
    "connections": ("id", "from", "to", "label", "variant"),
    "edges": ("id", "from", "to", "label", "variant"),
    "messages": ("id", "from", "to", "label", "variant", "kind"),
    "flows": ("id", "from", "to", "label", "variant"),
    "transitions": ("id", "from", "to", "label", "variant"),
    "boundaries": ("kind", "label", "wraps"),
    "lanes": ("id", "label"),
    "stages": ("id", "label"),
    "cards": ("dot", "title", "items"),
}

_META_FIELDS = ("title", "preset", "animation", "views", "locale", "quality_profile")
_TOP_FIELDS = ("schema_version", "diagram_type", "meta") + tuple(
    sorted({c for s in _SPEC.values() for c in s["collections"]})
)


def _node_kind_enum(diagram_type: str) -> tuple[str, ...]:
    return STATE_TYPES if diagram_type == "lifecycle" else NODE_TYPES


def _variant_enum(diagram_type: str) -> tuple[str, ...]:
    return SEQ_VARIANTS if diagram_type == "sequence" else VARIANTS


# ── 提示词片段（与上面的常量同源）────────────────────────────


def prompt_spec() -> str:
    """把 IR 契约渲染成提示词片段。

    ``courses.py`` 的 ``_LECTURE_PROMPT`` 用 ``__DIAGRAM_SPEC__`` 占位并由本函数注入——
    **契约只在上面定义一次**，校验器与提示词不可能各说一套。
    """
    return f"""- diagram 页：kind="diagram"，加字段 "diagram":{{"ir":<下面描述的 IR 对象>}}。
  **你只写结构化的 IR，绝不写 SVG / HTML / Mermaid 语法，也不要给任何坐标或样式字段**
  （出现 iris 之外的字段会被校验器拒绝并让你重写）。

  IR 的固定形状：
  {{"schema_version":{SCHEMA_VERSION},
    "diagram_type":"{' | '.join(DIAGRAM_TYPES)}",
    "meta":{{"title":"图题（≤{LIMITS['title']} 字）","preset":"{' | '.join(PRESETS)}",
             "views":[{{"id":"v1","label":"分步讲图的名字","focus":["节点id"],"note":"可选"}}]}},
    ...下面按 diagram_type 选对应数组}}

  选哪种图型（先判断这一页要讲清什么）：
  - workflow：有先后顺序的步骤、操作流程、审批、排错步骤 → 用 lanes + nodes + edges；
  - sequence：谁调用谁、调用与返回、请求响应 → 用 participants + messages；
  - dataflow：数据从哪来到哪去、经过哪些处理 → 用 stages + nodes + flows；
  - lifecycle：一个东西的状态变化、重试、终止态 → 用 lanes + states + transitions；
  - architecture：有哪些组件、分别属于哪一层/哪个边界 → 用 components + boundaries + connections。

  各图型的数组与字段（**只能出现这些字段**）：
  - workflow：["lanes"([{{"id","label"}}])], "nodes"([{{"id","type","label","sublabel","lane"}}]),
    "edges"([{{"id","from","to","label","variant"}}])；
  - sequence：["participants"([{{"id","type","label","sublabel"}}]),
    "messages"([{{"id","from","to","label","variant","kind"}}])]，kind 取 "call" 或 "return"；
  - dataflow：["stages"([{{"id","label"}}])], "nodes"([{{"id","type","label","sublabel","stage"}}]),
    "flows"([{{"id","from","to","label","variant"}}])；
  - lifecycle：["lanes"([{{"id","label"}}])], "states"([{{"id","type","label","sublabel","lane"}}]),
    "transitions"([{{"id","from","to","label","variant"}}])；
  - architecture：["components"([{{"id","type","label","sublabel","tag"}}]),
    "boundaries"([{{"kind","label","wraps"}}]), "connections"([{{"id","from","to","label","variant"}}])]。
  可选（任何图型都能加）："cards"([{{"dot","title","items"}}])，≤{LIMITS['cards']} 张。

  字段取值规则：
  - id：字母开头，只含字母/数字/下划线/短横线（如 n1、read-file、step2），**全图唯一**；
  - 节点 type：{' | '.join(NODE_TYPES)}
    （lifecycle 的 states[].type 用：{' | '.join(STATE_TYPES)}）；
  - variant：{' | '.join(VARIANTS)}（sequence 另可用 return），用来强调主路径(emphasis)
    或标出安全/权限相关的连接(security)或次要路径(dashed)；
  - relationships 的 from/to 必须是**上面已定义的节点 id**（写错会被拒绝并让你重写）；
  - boundaries.kind 取 region 或 security-group，wraps 里只能放已定义的组件 id；
  - 图题、节点标签、副标签一律简体中文；标签 ≤{LIMITS['label']} 字、副标签 ≤{LIMITS['sublabel']} 字，
    只写短语，不要整句。

  规模上限：节点 ≤{LIMITS['nodes']} 个、关系 ≤{LIMITS['relations']} 条、边界框 ≤{LIMITS['boundaries']} 个、
  分步讲图 ≤{LIMITS['views']} 个。**图中出现的每个实体都必须是材料里有的**，不要自己发明组件或步骤。"""


# ── 回执构造 ────────────────────────────────────────────────


class _Receipt:
    """收集诊断与修复建议（与 Archify 的 receipt 同构）。"""

    def __init__(self, diagram_type: str = "") -> None:
        self.diagram_type = diagram_type
        self.stage = "schema"
        self.diagnostics: list[dict[str, Any]] = []
        self.fixes: list[dict[str, Any]] = []

    def add(self, code: str, path: str, message: str, *, at: str = "",
            params: dict[str, Any] | None = None,
            evidence: dict[str, Any] | None = None, hint: str = "") -> None:
        self.diagnostics.append({
            "code": code, "path": path, "at": at, "message": message,
            "params": params or {}, "evidence": evidence or {},
        })
        if hint:
            self.fixes.append({"path": path, "code": code, "hint": hint})

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "diagram_type": self.diagram_type,
            "diagnostics": self.diagnostics,
            "supportedFixes": self.fixes,
        }


def _at_label(item: Any) -> str:
    """诊断里给人类看的定位注解（照 Archify 的 `(id/label: "...")` 风格）。"""
    if not isinstance(item, dict):
        return ""
    ident = str(item.get("id") or "").strip()
    label = str(item.get("label") or "").strip()
    if ident and label:
        return f'id/label: "{ident}"/"{label}"'
    if ident:
        return f'id: "{ident}"'
    if label:
        return f'label: "{label}"'
    return ""


# ── 校验（stage=schema / graph）─────────────────────────────


def _check_limits_text(rec: _Receipt, value: Any, path: str, at: str,
                       limit: int, field: str) -> None:
    text = str(value or "")
    if len(text) > limit:
        rec.add("schema/limit", path, f"{field} 超过 {limit} 字上限（当前 {len(text)} 字）",
                at=at, params={"limit": limit, "length": len(text)},
                hint=f"把该 {field} 压缩到 {limit} 字以内")


def validate(ir: Any) -> _Receipt:
    """校验 IR 的形状与图结构，返回回执（``ok=True`` 表示可编译）。"""
    rec = _Receipt()
    if not isinstance(ir, dict):
        rec.add("schema/type", "/", "diagram.ir 必须是一个 JSON 对象", hint="按契约重新给出 IR 对象")
        return rec

    dtype = str(ir.get("diagram_type") or "")
    rec.diagram_type = dtype

    # 顶层字段
    for key in ir:
        if key not in _TOP_FIELDS:
            rec.add("schema/unknown-field", f"/{key}",
                    f"IR 顶层不允许出现字段 {key!r}（不要给坐标/样式/SVG 相关内容）",
                    params={"field": key}, hint=f"删掉 {key} 字段")

    if ir.get("schema_version") != SCHEMA_VERSION:
        rec.add("schema/enum", "/schema_version",
                f"schema_version 必须是 {SCHEMA_VERSION}", params={"allowed": [SCHEMA_VERSION]},
                hint=f"写成 {SCHEMA_VERSION}")
    if dtype not in DIAGRAM_TYPES:
        rec.add("schema/enum", "/diagram_type",
                f"diagram_type 必须是 {' / '.join(DIAGRAM_TYPES)} 之一",
                params={"allowed": list(DIAGRAM_TYPES)},
                hint=f"从 {' / '.join(DIAGRAM_TYPES)} 里选一个")
        return rec

    spec = _SPEC[dtype]
    meta = ir.get("meta")
    if not isinstance(meta, dict):
        rec.add("schema/required", "/meta", "缺少 meta 对象", hint='补 "meta":{"title":"..."}')
    else:
        for key in meta:
            if key not in _META_FIELDS:
                rec.add("schema/unknown-field", f"/meta/{key}",
                        f"meta 不允许出现字段 {key!r}", params={"field": key},
                        hint=f"删掉 meta.{key}")
        title = str(meta.get("title") or "").strip()
        if not title:
            rec.add("schema/required", "/meta/title", "缺少图题 meta.title",
                    hint="写一个 ≤24 字的图题")
        else:
            _check_limits_text(rec, title, "/meta/title", "", LIMITS["title"], "图题")
        preset = meta.get("preset")
        if preset is not None and preset not in PRESETS:
            rec.add("schema/enum", "/meta/preset",
                    f"preset 只能是 {' / '.join(PRESETS)} 之一",
                    params={"allowed": list(PRESETS)}, hint="选一个预设，或整个删掉")
        anim = meta.get("animation")
        if anim not in (None, "none"):
            rec.add("schema/enum", "/meta/animation",
                    'animation 只支持 "none"（本产品不做动效）',
                    params={"allowed": ["none"]}, hint="删掉 animation 字段")

    # 集合存在性与未知集合
    for key in ir:
        if key in ("schema_version", "diagram_type", "meta"):
            continue
        if key not in spec["collections"]:
            rec.add("schema/unknown-field", f"/{key}",
                    f"{dtype} 图不允许出现数组 {key!r}",
                    params={"field": key, "allowed": list(spec["collections"])},
                    hint=f"删掉 {key}（本题型允许：{' / '.join(spec['collections'])}）")
    for key in spec["required"]:
        if not isinstance(ir.get(key), list) or not ir.get(key):
            rec.add("schema/required", f"/{key}",
                    f"{dtype} 图必须给出非空数组 {key}",
                    hint=f"补上 {key} 数组")

    # 逐集合校验字段与取值
    for coll in spec["collections"]:
        items = ir.get(coll)
        if items is None:
            continue
        if not isinstance(items, list):
            rec.add("schema/type", f"/{coll}", f"{coll} 必须是数组", hint="改成数组")
            continue
        allowed_fields = _FIELDS[coll]
        node_coll = coll == spec["nodes"]
        rel_coll = coll == spec["relations"]
        kind_enum = _node_kind_enum(dtype)
        variant_enum = _variant_enum(dtype)
        for i, item in enumerate(items):
            path = f"/{coll}/{i}"
            at = _at_label(item)
            if not isinstance(item, dict):
                rec.add("schema/type", path, f"{coll}[{i}] 必须是对象", hint="改成对象")
                continue
            for key in item:
                if key not in allowed_fields:
                    rec.add("schema/unknown-field", f"{path}/{key}",
                            f"{coll} 的元素不允许字段 {key!r}"
                            + ("（不要给坐标 pos/size/via 或样式字段）"
                               if key in ("pos", "size", "via", "labelAt", "colour", "color", "style")
                               else ""),
                            at=at, params={"field": key, "allowed": list(allowed_fields)},
                            hint=f"删掉 {key}")
            # id：节点/关系/分组必须有；boundaries 与 cards 不需要
            # （Archify 里 boundaries 的身份由 `kind + label` 派生，cards 无所谓）
            if coll in (spec["nodes"], spec["relations"], "lanes", "stages"):
                ident = item.get("id")
                if ident is None:
                    rec.add("schema/required", f"{path}/id", f"{coll}[{i}] 缺少 id", at=at,
                            hint="给一个字母开头的唯一 id")
                elif not isinstance(ident, str) or not _ID_RE.match(ident):
                    rec.add("schema/pattern", f"{path}/id",
                            "id 必须字母开头、只含字母数字下划线短横线",
                            at=at, params={"value": ident, "pattern": _ID_RE.pattern},
                            hint="改用形如 n1 / read-file 的 id")
            # label / sublabel / title
            if node_coll:
                label = str(item.get("label") or "").strip()
                if not label:
                    rec.add("schema/required", f"{path}/label", f"{coll}[{i}] 缺少 label",
                            at=at, hint="给节点写一个 ≤14 字的标签")
                else:
                    _check_limits_text(rec, label, f"{path}/label", at, LIMITS["label"], "节点标签")
                if item.get("sublabel"):
                    _check_limits_text(rec, item["sublabel"], f"{path}/sublabel", at,
                                       LIMITS["sublabel"], "副标签")
                ntype = str(item.get("type") or "")
                if ntype and ntype not in kind_enum:
                    rec.add("schema/enum", f"{path}/type",
                            f"type 取值非法", at=at, params={"allowed": list(kind_enum)},
                            hint=f"从 {' / '.join(kind_enum)} 里选一个")
            if rel_coll:
                for side in ("from", "to"):
                    if not str(item.get(side) or "").strip():
                        rec.add("schema/required", f"{path}/{side}",
                                f"{coll}[{i}] 缺少 {side}", at=at,
                                hint=f"写上一个已定义的节点 id")
                variant = str(item.get("variant") or "default")
                if variant not in variant_enum:
                    rec.add("schema/enum", f"{path}/variant", "variant 取值非法", at=at,
                            params={"allowed": list(variant_enum)},
                            hint=f"从 {' / '.join(variant_enum)} 里选一个（或整条删掉）")
                if coll == "messages" and item.get("kind") not in (None, "call", "return"):
                    rec.add("schema/enum", f"{path}/kind", 'messages[].kind 只能是 "call" 或 "return"',
                            at=at, params={"allowed": ["call", "return"]},
                            hint="改成 call 或 return")
            if coll in ("lanes", "stages"):
                label = str(item.get("label") or "").strip()
                if not label:
                    rec.add("schema/required", f"{path}/label", f"{coll}[{i}] 缺少 label",
                            at=at, hint="给分组写一个短标签")
                else:
                    _check_limits_text(rec, label, f"{path}/label", at, LIMITS["label"], "分组标签")
            if coll == "boundaries":
                bkind = str(item.get("kind") or "")
                if bkind not in BOUNDARY_KINDS:
                    rec.add("schema/enum", f"{path}/kind",
                            "boundaries[].kind 只能是 region 或 security-group", at=at,
                            params={"allowed": list(BOUNDARY_KINDS)}, hint="改成 region 或 security-group")
                if not str(item.get("label") or "").strip():
                    rec.add("schema/required", f"{path}/label", "boundaries 缺少 label", at=at,
                            hint="给边界写一个短标签")
                if not isinstance(item.get("wraps"), list) or not item["wraps"]:
                    rec.add("schema/required", f"{path}/wraps",
                            "boundaries 必须给出非空 wraps（要圈住的组件 id）", at=at,
                            hint="写上被圈住的组件 id")
            if coll == "cards":
                if not str(item.get("title") or "").strip():
                    rec.add("schema/required", f"{path}/title", "cards 缺少 title", at=at,
                            hint="给卡片写个标题")
                if not isinstance(item.get("items"), list) or not item["items"]:
                    rec.add("schema/required", f"{path}/items", "cards 缺少非空 items", at=at,
                            hint="写上 1~3 条要点")
                dot = item.get("dot")
                if dot is not None and dot not in CARD_DOTS:
                    rec.add("schema/enum", f"{path}/dot", "cards[].dot 取值非法", at=at,
                            params={"allowed": list(CARD_DOTS)}, hint="换一个颜色名或删掉 dot")

        if len(items) > (LIMITS["nodes"] if node_coll else
                         LIMITS["relations"] if rel_coll else
                         LIMITS.get(coll, 99)):
            cap = (LIMITS["nodes"] if node_coll else
                   LIMITS["relations"] if rel_coll else LIMITS.get(coll, 99))
            if cap < 99:
                rec.add("schema/limit", f"/{coll}",
                        f"{coll} 共 {len(items)} 项，超过上限 {cap}",
                        params={"limit": cap, "count": len(items)},
                        hint=f"把 {coll} 精简到 {cap} 项以内")

    if not rec.ok:
        return rec

    # ── 跨集合事实（JSON Schema 表达不了的那一层）──
    rec.stage = "graph"
    node_coll, rel_coll = spec["nodes"], spec["relations"]
    nodes = ir.get(node_coll) or []
    rels = ir.get(rel_coll) or []
    ids = [str(n.get("id")) for n in nodes]
    id_set = set(ids)
    if len(id_set) != len(ids):
        dup = sorted({x for x in ids if ids.count(x) > 1})
        rec.add("graph/duplicate-id", f"/{node_coll}", f"{node_coll} 里有重复 id：{', '.join(dup)}",
                params={"duplicates": dup}, evidence={"ids": ids},
                hint="把重复的 id 改成唯一值，并同步改引用它的关系")
    rel_ids = [str(r.get("id")) for r in rels if r.get("id")]
    if len(set(rel_ids)) != len(rel_ids):
        dup = sorted({x for x in rel_ids if rel_ids.count(x) > 1})
        rec.add("graph/duplicate-relation-id", f"/{rel_coll}",
                f"{rel_coll} 里有重复 id：{', '.join(dup)}",
                params={"duplicates": dup}, hint="关系 id 也要唯一")
    for i, r in enumerate(rels):
        path = f"/{rel_coll}/{i}"
        at = _at_label(r)
        for side in ("from", "to"):
            val = str(r.get(side) or "")
            if val and val not in id_set:
                rec.add("graph/dangling-ref", f"{path}/{side}",
                        f"{side} 指向不存在的节点 id",
                        at=at, params={"field": side, "value": val},
                        evidence={"known_ids": sorted(id_set)},
                        hint="改成上面 known_ids 里的 id，或把这个节点补进 nodes")
        if str(r.get("from") or "") and str(r.get("from")) == str(r.get("to")):
            rec.add("graph/self-loop", path, "关系不能自己指向自己", at=at,
                    hint="删掉这条关系，或改成指向另一个节点")
    # 孤点（sequence 的参与者可以只出现在一条消息里，仍可能天然孤立，同样提示）
    if len(nodes) > 1:
        touched = {str(r.get("from")) for r in rels} | {str(r.get("to")) for r in rels}
        orphans = [str(n.get("id")) for n in nodes if str(n.get("id")) not in touched]
        if orphans:
            rec.add("graph/orphan-node", f"/{node_coll}",
                    f"这些节点没有任何关系指向或发出：{', '.join(orphans)}",
                    params={"orphans": orphans}, evidence={"ids": ids},
                    hint="给它们补上关系，或从 nodes 里删掉（图里不该有孤立内容）")
    for coll in ("boundaries",):
        for i, b in enumerate(ir.get(coll) or []):
            for j, w in enumerate(b.get("wraps") or []):
                if str(w) not in id_set:
                    rec.add("graph/dangling-ref", f"/{coll}/{i}/wraps/{j}",
                            "wraps 指向不存在的组件 id", at=_at_label(b),
                            params={"value": w}, evidence={"known_ids": sorted(id_set)},
                            hint="改成已定义的组件 id")
    for i, v in enumerate((meta or {}).get("views") or []):
        if not isinstance(v, dict):
            continue
        for j, f in enumerate(v.get("focus") or []):
            if str(f) not in id_set:
                rec.add("view/unknown-focus", f"/meta/views/{i}/focus/{j}",
                        "views.focus 指向不存在的节点 id",
                        at=str(v.get("id") or ""), params={"value": f},
                        evidence={"known_ids": sorted(id_set)},
                        hint="focus 只能用图里已有的节点 id")
    return rec


# ── 确定性布局 ──────────────────────────────────────────────

_FONT = 13.0
_SUB_FONT = 11.0
_NODE_H = 44.0
_NODE_H_SUB = 58.0
_COL_GAP = 58.0
_ROW_GAP = 30.0
_PAD = 18.0
_BOX_MIN_W = 96.0
_BOX_MAX_W = 220.0
_SEQ_LINE_H = 46.0


def _text_units(s: str) -> float:
    """确定性的文本宽度估算（CJK 计 1.0、其余计 0.55 个字宽）。

    刻意不用 canvas 测量：同一份 IR 必须每次都编译出**逐字节相同**的 SVG，
    否则「确定性编译」和基于快照的回归测试都无从谈起。
    """
    total = 0.0
    for ch in str(s or ""):
        total += 1.0 if ord(ch) > 0x2E80 else 0.55
    return total


def _box_w(label: str, sublabel: str, tag: str) -> float:
    w = max(_text_units(label) * _FONT,
            _text_units(sublabel) * _SUB_FONT,
            _text_units(tag) * _SUB_FONT)
    return max(_BOX_MIN_W, min(_BOX_MAX_W, w + 28.0))


def _topo_columns(nodes: list[dict[str, Any]], rels: list[dict[str, Any]]) -> list[list[dict]]:
    """无分组信息时按「入度为 0 先排」的确定性分层（同层按声明顺序）。

    ⚠️ 用 BFS 首达分层，**不能**改成「不断放宽层号」的写法：
    模型可以产出带环的图（校验器不拒绝环，环是合法语义，比如「重试」），
    放宽写法在环上层号无限增长 → 死循环（实测踩到：workflow 单 lane 进来这里挂死）。
    """
    ids = [str(n["id"]) for n in nodes]
    indeg = {i: 0 for i in ids}
    outs: dict[str, list[str]] = {i: [] for i in ids}
    for r in rels:
        a, b = str(r.get("from")), str(r.get("to"))
        if a in indeg and b in indeg:
            indeg[b] += 1
            outs[a].append(b)
    frontier = [i for i in ids if indeg[i] == 0] or ids[:1]
    layer = {i: 0 for i in frontier}
    queue = list(frontier)
    seen = set(frontier)
    while queue:
        cur = queue.pop(0)
        for nxt in outs.get(cur, []):
            if nxt in seen:
                continue                      # 环免疫：每个节点只入队一次
            seen.add(nxt)
            layer[nxt] = layer[cur] + 1
            queue.append(nxt)
    for i in ids:
        layer.setdefault(i, 0)
    max_layer = max(layer.values()) if layer else 0
    cols: list[list[dict]] = [[] for _ in range(max_layer + 1)]
    for n in nodes:
        cols[layer[str(n["id"])]].append(n)
    return [c for c in cols if c]


def _group_columns(ir: dict[str, Any], spec: dict[str, Any]) -> list[tuple[str, list[dict]]]:
    """按 list/type/stage 分列；没有分组信息则退回拓扑分层。"""
    nodes = list(ir.get(spec["nodes"]) or [])
    rels = list(ir.get(spec["relations"]) or [])
    gb = spec.get("group_by")
    if gb:
        groups = list(ir.get({"type": "lanes", "lane": "lanes",
                              "stage": "stages"}.get(gb, "")) or [])
        if groups:
            out: list[tuple[str, list[dict]]] = []
            used: set[str] = set()
            for g in groups:
                gid = str(g.get("id"))
                members = [n for n in nodes if str(n.get(gb)) == gid]
                if members:
                    out.append((str(g.get("label") or gid), members))
                    used.update(str(m["id"]) for m in members)
            rest = [n for n in nodes if str(n["id"]) not in used]
            if rest:
                out.append(("", rest))
            # 只分出一个组 = 分组没有提供任何信息（模型常声明单个 lane 把整条
            # 流程装进去）→ 沿一列纵向堆叠，右侧大片留白。退回拓扑分层，
            # 让 A→B→C 链横向铺开用满宽度。
            if len(out) == 1 and len(nodes) >= 2:
                return [("", c) for c in _topo_columns(nodes, rels)]
            return out
    if spec["group_by"] == "type":
        order = NODE_TYPES
        out = []
        for t in order:
            members = [n for n in nodes if str(n.get("type") or "backend") == t]
            if members:
                out.append((t, members))
        if len(out) == 1 and len(nodes) >= 2:
            # 同理：全部组件同属一个类型时，按拓扑分层横向铺开
            return [("", c) for c in _topo_columns(nodes, rels)]
        return out
    return [("", c) for c in _topo_columns(nodes, rels)]


def _layout_columns(cols: list[tuple[str, list[dict]]]) -> tuple[dict[str, dict], float, float]:
    """把分列结果落成几何坐标，返回 (位置表, 宽, 高)。"""
    pos: dict[str, dict] = {}
    x = _PAD
    max_h = 0.0
    for _label, members in cols:
        width = max(_box_w(str(m.get("label") or ""), str(m.get("sublabel") or ""),
                           str(m.get("tag") or "")) for m in members)
        y = _PAD
        for m in members:
            h = _NODE_H_SUB if (m.get("sublabel") or m.get("tag")) else _NODE_H
            pos[str(m["id"])] = {"x": x, "y": y, "w": width, "h": h, "node": m}
            y += h + _ROW_GAP
        max_h = max(max_h, y - _ROW_GAP + _PAD)
        x += width + _COL_GAP
    return pos, x - _COL_GAP + _PAD, max_h


def _layout_sequence(participants: list[dict], messages: list[dict]) -> tuple[dict, float, float]:
    pos: dict[str, dict] = {}
    x = _PAD
    top = _PAD
    for p in participants:
        w = _box_w(str(p.get("label") or ""), str(p.get("sublabel") or ""), "")
        pos[str(p["id"])] = {"x": x, "y": top, "w": w, "h": _NODE_H, "node": p}
        x += w + _COL_GAP
    width = x - _COL_GAP + _PAD
    height = top + _NODE_H + 22 + _SEQ_LINE_H * max(1, len(messages)) + _PAD
    return pos, width, height


# ── SVG 渲染 ────────────────────────────────────────────────

_PRESET_CSS: dict[str, dict[str, str]] = {
    "classic": {
        "node_fill": "#eef2ff", "node_stroke": "#c7d2fe", "text": "#1f2430", "sub": "#6b7280",
        "edge": "#94a3b8", "label": "#475569", "boundary": "#cbd5e1", "boundary_fill": "#f8fafc",
    },
    "signal-flow": {
        "node_fill": "#e0f2fe", "node_stroke": "#7dd3fc", "text": "#0c4a6e", "sub": "#0369a1",
        "edge": "#38bdf8", "label": "#0c4a6e", "boundary": "#bae6fd", "boundary_fill": "#f0f9ff",
    },
    "blueprint": {
        "node_fill": "#eff6ff", "node_stroke": "#1d4ed8", "text": "#172554", "sub": "#1e40af",
        "edge": "#1d4ed8", "label": "#1e3a8a", "boundary": "#93c5fd", "boundary_fill": "#f8fbff",
    },
    "editorial": {
        "node_fill": "#fffbf5", "node_stroke": "#d6c3a5", "text": "#3f3222", "sub": "#8a7559",
        "edge": "#b08968", "label": "#6b5233", "boundary": "#e0cfb4", "boundary_fill": "#fffdf9",
    },
}
_EMPHASIS = {"emphasis": "#4f46e5", "security": "#dc2626", "return": "#0d9488"}


def _render_svg(ir: dict[str, Any], dtype: str, pos: dict[str, dict],
                width: float, height: float, preset: str) -> str:
    p = _PRESET_CSS.get(preset) or _PRESET_CSS["classic"]
    rels = list(ir.get(_SPEC[dtype]["relations"]) or [])
    body: list[str] = []

    # 边界框（先画，压在最底层）
    for b in ir.get("boundaries") or []:
        boxes = [pos[str(w)] for w in (b.get("wraps") or []) if str(w) in pos]
        if not boxes:
            continue
        x0 = min(bx["x"] for bx in boxes) - 12
        y0 = min(bx["y"] for bx in boxes) - 26
        x1 = max(bx["x"] + bx["w"] for bx in boxes) + 12
        y1 = max(bx["y"] + bx["h"] for bx in boxes) + 12
        body.append(
            f'<g class="zf-boundary"><rect x="{x0:.1f}" y="{y0:.1f}" '
            f'width="{x1 - x0:.1f}" height="{y1 - y0:.1f}" rx="10" '
            f'fill="{p["boundary_fill"]}" stroke="{p["boundary"]}" stroke-width="1" '
            f'stroke-dasharray="5 3"/>'
            f'<text x="{x0 + 10:.1f}" y="{y0 + 17:.1f}" font-size="11" '
            f'fill="{p["sub"]}">{_esc(str(b.get("label") or ""))}</text></g>'
        )

    # 时序图：先画每个参与者的生命线（消息箭头挂在它上面）
    if dtype == "sequence":
        bottom = max(120.0, height) - _PAD
        for _ident, box in pos.items():
            cx = box["x"] + box["w"] / 2
            y0 = box["y"] + box["h"]
            body.append(
                f'<line class="zf-lifeline" x1="{cx:.1f}" y1="{y0:.1f}" '
                f'x2="{cx:.1f}" y2="{bottom:.1f}" stroke="{p["boundary"]}" '
                f'stroke-width="1" stroke-dasharray="4 4"/>'
            )

    # 关系
    for ri, r in enumerate(rels):
        a, b = pos.get(str(r.get("from"))), pos.get(str(r.get("to")))
        if not a or not b:
            continue
        variant = str(r.get("variant") or "default")
        color = _EMPHASIS.get(variant, p["edge"])
        dash = ' stroke-dasharray="4 3"' if variant == "dashed" else ""
        width_attr = "2.4" if variant in _EMPHASIS else "1.6"
        if dtype == "sequence":
            y = b["y"] + 40 + _SEQ_LINE_H * ri
            x0 = a["x"] + a["w"] / 2
            x1 = b["x"] + b["w"] / 2
            back = str(r.get("kind") or "") == "return"
            tip = x0 if back else x1
            line = (f'<path d="M {x0:.1f} {y:.1f} L {tip:.1f} {y:.1f}" fill="none" '
                    f'stroke="{color}" stroke-width="{width_attr}" marker-end="url(#zf-arrow)"{dash}/>')
            mid = (x0 + x1) / 2
            label = str(r.get("label") or "")
            text = (f'<text x="{mid:.1f}" y="{y - 5:.1f}" text-anchor="middle" '
                    f'font-size="11" fill="{p["label"]}" paint-order="stroke" '
                    f'stroke="#ffffff" stroke-width="3">{_esc(label)}</text>') if label else ""
            body.append(f'<g class="zf-edge">{line}{text}</g>')
            continue
        # 分层图：优先左→右，否则上→下
        if b["x"] > a["x"] + a["w"] - 1:
            x0, y0 = a["x"] + a["w"], a["y"] + a["h"] / 2
            x1, y1 = b["x"], b["y"] + b["h"] / 2
            dx = max(18.0, (x1 - x0) / 2)
            d = f"M {x0:.1f} {y0:.1f} C {x0 + dx:.1f} {y0:.1f}, {x1 - dx:.1f} {y1:.1f}, {x1:.1f} {y1:.1f}"
            lx, ly = (x0 + x1) / 2, (y0 + y1) / 2 - 6
        else:
            x0, y0 = a["x"] + a["w"] / 2, a["y"] + a["h"]
            x1, y1 = b["x"] + b["w"] / 2, b["y"]
            d = f"M {x0:.1f} {y0:.1f} C {x0:.1f} {y0 + 20:.1f}, {x1:.1f} {y1 - 20:.1f}, {x1:.1f} {y1:.1f}"
            lx, ly = (x0 + x1) / 2 + 6, (y0 + y1) / 2
        body.append(
            f'<g class="zf-edge"><path d="{d}" fill="none" stroke="{color}" '
            f'stroke-width="{width_attr}" marker-end="url(#zf-arrow)"{dash}/>'
            + (f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" font-size="11" '
               f'fill="{p["label"]}" paint-order="stroke" stroke="#ffffff" stroke-width="3">'
               f'{_esc(str(r.get("label") or ""))}</text>' if r.get("label") else "")
            + "</g>"
        )

    # 节点
    for ident, box in pos.items():
        n = box["node"]
        label = _esc(str(n.get("label") or ""))
        sub = str(n.get("sublabel") or n.get("tag") or "")
        cx = box["x"] + box["w"] / 2
        if sub:
            ly, sy = box["y"] + box["h"] / 2 - 3, box["y"] + box["h"] / 2 + 13
        else:
            ly, sy = box["y"] + box["h"] / 2 + 4.5, 0
        body.append(
            f'<g class="zf-node" data-id="{_esc(ident)}">'
            f'<rect x="{box["x"]:.1f}" y="{box["y"]:.1f}" width="{box["w"]:.1f}" '
            f'height="{box["h"]:.1f}" rx="8" fill="{p["node_fill"]}" '
            f'stroke="{p["node_stroke"]}" stroke-width="1.4"/>'
            f'<text x="{cx:.1f}" y="{ly:.1f}" text-anchor="middle" font-size="{_FONT:.0f}" '
            f'fill="{p["text"]}" font-weight="600">{label}</text>'
            + (f'<text x="{cx:.1f}" y="{sy:.1f}" text-anchor="middle" '
               f'font-size="{_SUB_FONT:.0f}" fill="{p["sub"]}">{_esc(sub)}</text>' if sub else "")
            + "</g>"
        )

    w = max(240.0, width)
    h = max(120.0, height)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" '
        f'width="100%" role="img" class="zf-svg" data-diagram="{_esc(dtype)}" '
        f'data-preset="{_esc(preset)}" aria-label="{_esc(str((ir.get("meta") or {}).get("title") or ""))}">'
        f'<defs><marker id="zf-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{p["edge"]}"/></marker></defs>'
        + "".join(body) + "</svg>"
    )


# ── 对外入口 ────────────────────────────────────────────────


def compile_ir(ir: Any) -> tuple[str | None, dict[str, Any]]:
    """编译 IR：成功返回 ``(svg, receipt)``，失败返回 ``(None, receipt)``。

    ``receipt`` 结构与 Archify 一致，``ok=False`` 时 ``diagnostics``
    给出可执行的修复指引（稳定规则码 + 路径 + 最近的 id/label + 证据）。
    """
    rec = validate(ir)
    if not rec.ok:
        return None, rec.to_dict()
    dtype = str(ir["diagram_type"])
    spec = _SPEC[dtype]
    preset = str((ir.get("meta") or {}).get("preset") or "classic")
    if dtype == "sequence":
        pos, w, h = _layout_sequence(list(ir.get("participants") or []),
                                     list(ir.get("messages") or []))
    else:
        pos, w, h = _layout_columns(_group_columns(ir, spec))
    rec.stage = "layout"
    try:
        svg = _render_svg(ir, dtype, pos, w, h, preset)
    except Exception as exc:  # noqa: BLE001 - 编译器自身异常也算失败，走回退
        rec.add("layout/render-failed", "/", f"编译图示失败：{type(exc).__name__}",
                hint="简化这张图（节点/关系更少、标签更短）后重试")
        return None, rec.to_dict()
    return svg, rec.to_dict()
