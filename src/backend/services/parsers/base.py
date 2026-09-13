"""解析器基类与数据模型。

:class:`Block` 是「解析 → 切片」之间的唯一中间产物：每个 Block 携带
``text`` 与**权威元数据** ``page_no / section / anchor``。
切片阶段据此保留页码，引用阶段据此回填页码——页码**从不由模型生成**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Block", "make_anchor"]


def make_anchor(
    text: str,
    *,
    char_start: int = 0,
    slide: int | None = None,
    heading: str | None = None,
) -> dict[str, Any]:
    """构造 anchor 字典（用于前端定位高亮）。

    Args:
        text: 片段文本。
        char_start: 片段在所属页/节内的起始字符偏移。
        slide: 幻灯片序号（PPTX 用）。
        heading: 所属标题。

    Returns:
        ``{"char_start", "char_end", "slide", "heading"}``。
    """
    return {
        "char_start": int(char_start),
        "char_end": int(char_start) + len(text or ""),
        "slide": slide,
        "heading": heading,
    }


@dataclass(slots=True)
class Block:
    """解析出的一个文本块（段落 / 页面 / 标题块）。

    Attributes:
        text: 块文本（已清洗）。
        page_no: 页码或幻灯片序号（1 起）；无页码时为 ``None``。
        section: 章节/标题路径（如 ``"3.2 洛必达法则"``）。
        heading: 本块自身的标题（若为标题块）。
        anchor: 定位信息，见 :func:`make_anchor`。
        order: 块在文档中的顺序（0 起）。
    """

    text: str
    page_no: int | None = None
    section: str | None = None
    heading: str | None = None
    anchor: dict[str, Any] = field(default_factory=dict)
    order: int = 0
