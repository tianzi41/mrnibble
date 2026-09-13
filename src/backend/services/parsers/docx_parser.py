"""DOCX 解析器：python-docx 逐段落解析，保留标题层级作为 ``section``。"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import Block, make_anchor
from ...utils.textutil import clean_text

logger = logging.getLogger(__name__)

__all__ = ["parse"]


def _is_heading(style_name: str, style_id: str) -> bool:
    """判断段落样式是否为标题（兼容中英文样式名）。"""
    name = (style_name or "").lower()
    sid = (style_id or "").lower()
    return (
        name.startswith("heading")
        or name.startswith("title")
        or "标题" in (style_name or "")
        or sid.startswith("heading")
    )


def parse(path: Path, **_: object) -> list[Block]:
    """逐段落解析 DOCX。

    Args:
        path: DOCX 文件路径。
        **_: 兼容统一签名，忽略多余参数。

    Returns:
        ``Block`` 列表；标题段落更新 ``section``，正文段落继承当前 ``section``。

    Raises:
        RuntimeError: 文件打开失败。
    """
    from docx import Document as DocxDocument

    try:
        document = DocxDocument(str(path))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"DOCX 打开失败：{type(exc).__name__}") from exc

    blocks: list[Block] = []
    order = 0
    heading_stack: list[str] = []
    char_cursor = 0

    for para in document.paragraphs:
        text = clean_text(para.text or "")
        if not text:
            continue
        style = para.style
        style_name = getattr(style, "name", "") or ""
        style_id = getattr(style, "style_id", "") or ""

        if _is_heading(style_name, style_id):
            # 用标题层级维护 section 路径。
            level = _heading_level(style_name, style_id)
            while len(heading_stack) >= level:
                heading_stack.pop()
            heading_stack.append(text)
            section = " / ".join(heading_stack)
            block = Block(
                text=text,
                page_no=None,
                section=section,
                heading=text,
                anchor=make_anchor(text, char_start=char_cursor, heading=text),
                order=order,
            )
        else:
            section = " / ".join(heading_stack) or None
            block = Block(
                text=text,
                page_no=None,
                section=section,
                heading=None,
                anchor=make_anchor(text, char_start=char_cursor, heading=section),
                order=order,
            )
        char_cursor += len(text) + 1
        blocks.append(block)
        order += 1

    logger.info("DOCX 解析完成", extra={"extra_fields": {"blocks": len(blocks)}})
    return blocks


def _heading_level(style_name: str, style_id: str) -> int:
    """从样式名解析标题层级（``Heading 2``/``标题 2`` → 2），默认 1。"""
    import re

    for source in (style_id, style_name):
        match = re.search(r"(\d+)", source or "")
        if match:
            try:
                return max(1, min(int(match.group(1)), 9))
            except ValueError:
                continue
    return 1


def page_count(path: Path) -> int:
    """DOCX 无固定页数，返回 0（由切片数量体现规模）。"""
    return 0
