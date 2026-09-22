"""用户画像上下文：把引导收集的 ``profile.*`` 设置拼成一段「给模型看的用户背景」。

所有 AI 生成路径（课程 / 讲义 / 练习 / 聊天 / AI 写材料）在发请求前调用
:func:`with_profile`，把画像放到系统提示词最前面。画像全空时返回空串，
调用方原样使用即可（不改变任何既有行为）。

设计约束：
- 存储的是**选项 id**（英文短 id，前端 ``js/profile_fields.js`` 的 FIELDS
  与此处 :data:`_LABELS` / :data:`_VALUE_TEXT` 一一对应），注入时转中文；
- 只描述用户**明确填过**的项，未填的不臆造、不留空行；
- 段落固定以「不要臆造」收尾，避免模型拿着半份画像自由发挥。
"""

from __future__ import annotations

from typing import Any

from ..deps import get_settings_service

__all__ = ["profile_context", "with_profile"]

# 字段 → 中文标签（按展示顺序）
_LABELS: dict[str, str] = {
    "age": "年龄",
    "role": "身份",
    "stage": "就读阶段",
    "grade": "年级",
    "purpose": "主要学习目的",
    "style": "喜欢的讲法",
    "daily": "每天能投入",
    "fields": "常学领域",
    "note": "补充说明",
}

# 选项 id → 中文描述（与前端 FIELDS 的选项一致）
_VALUE_TEXT: dict[str, dict[str, str]] = {
    "age": {"u18": "18 岁以下", "18-25": "18–25 岁", "26-35": "26–35 岁",
            "36-50": "36–50 岁", "50p": "50 岁以上"},
    "role": {"student": "学生", "worker": "职场人", "freelance": "自由职业",
             "other": "其他"},
    "stage": {"primary": "小学", "junior": "初中", "senior": "高中",
              "university": "大学", "postgrad": "研究生"},
    "grade": {"p1": "小学一年级", "p2": "小学二年级", "p3": "小学三年级",
              "p4": "小学四年级", "p5": "小学五年级", "p6": "小学六年级",
              "j1": "初一", "j2": "初二", "j3": "初三",
              "s1": "高一", "s2": "高二", "s3": "高三",
              "u1": "大一", "u2": "大二", "u3": "大三", "u4": "大四",
              "pg": "研究生"},
    "purpose": {"exam": "应付考试", "cert": "考证 / 考研", "work": "提升工作技能",
                "interest": "兴趣拓展", "kids": "辅导孩子"},
    "style": {"analogy": "多举例、打比方", "rigor": "按公式严谨推导",
              "exampoints": "直接划考点", "quick": "快速过一遍"},
    "daily": {"d15": "15 分钟以内", "d30": "半小时左右", "d60": "1 小时左右",
              "flex": "不固定"},
    "fields": {"code": "编程 / 计算机", "en": "英语", "math": "数学", "lit": "文史",
               "sci": "理化", "work": "职业技能", "art": "艺术 / 设计", "other": "其他"},
}

_FIELD_ORDER = ["age", "role", "stage", "grade", "purpose", "style",
                "daily", "fields", "note"]


def _human(field: str, raw: str) -> str:
    """选项 id → 中文描述；fields 是多选（逗号分隔），逐个翻译。

    没有翻译表的字段（开放题 note、以及未来新增时忘了建表的字段）**原样返回**——
    静默丢弃会让用户填了内容却毫无效果，且从界面上看不出来。
    """
    table = _VALUE_TEXT.get(field)
    if table is None:
        return raw.strip()
    if field == "fields":
        parts = [table.get(p.strip(), "") for p in raw.split(",")]
        return "、".join(p for p in parts if p)
    return table.get(raw.strip(), "")


def profile_context() -> str:
    """读取 ``profile.*`` 设置，拼一段中文用户背景；全空返回空串。"""
    svc = get_settings_service()
    lines: list[str] = []
    for field in _FIELD_ORDER:
        raw = (svc.get("profile." + field) or "").strip()
        if not raw:
            continue
        text = _human(field, raw)
        if not text:
            continue
        lines.append(f"- {_LABELS[field]}：{text}")
    if not lines:
        return ""
    return ("【用户背景】用户在新手引导里填写的学习画像（用于调整讲解难度、"
            "举例方式与内容取舍；用户没填的方面不要臆造）\n" + "\n".join(lines))


def with_profile(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """把画像上下文放到系统提示词**最前面**（没有 system 消息则插入一条）。

    返回新列表，不修改入参（调用方可能复用 messages）。
    """
    ctx = profile_context()
    if not ctx:
        return messages
    out: list[dict[str, Any]] = [dict(m) for m in messages]
    if out and out[0].get("role") == "system":
        out[0]["content"] = ctx + "\n\n" + str(out[0].get("content") or "")
    else:
        out.insert(0, {"role": "system", "content": ctx})
    return out
