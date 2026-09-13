"""纯文本 / Markdown 解析器。

- ``txt``：整体作为一个块（按空行分段）。
- ``md``：解析 ATX 标题（``#``~``######``）维护 ``section`` 路径，其余为正文段落。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .base import Block, make_anchor
from ...utils.textutil import clean_text

logger = logging.getLogger(__name__)

__all__ = ["parse_txt", "parse_md", "parse"]

_ATX = re.compile(r"^(#{1,6})\s+(.*)$")


def _read_text(path: Path) -> str:
    """以 UTF-8 读取文本文件，失败时回退 GBK（兼容国内导出的 txt）。"""
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def parse_txt(path: Path, **_: object) -> list[Block]:
    """解析 TXT：按空行分段，每段一个块。"""
    content = clean_text(_read_text(path))
    blocks: list[Block] = []
    cursor = 0
    for para in content.split("\n\n"):
        text = clean_text(para)
        if not text:
            continue
        blocks.append(
            Block(
                text=text,
                page_no=None,
                section=None,
                anchor=make_anchor(text, char_start=cursor),
                order=len(blocks),
            )
        )
        cursor += len(text) + 2
    logger.info("TXT 解析完成", extra={"extra_fields": {"blocks": len(blocks)}})
    return blocks


def parse_md(path: Path, **_: object) -> list[Block]:
    """解析 Markdown：ATX 标题维护 section 路径，正文按空行分块。"""
    content = clean_text(_read_text(path))
    blocks: list[Block] = []
    heading_stack: list[str] = []
    cursor = 0

    for raw_line in content.split("\n"):
        line = raw_line.rstrip()
        match = _ATX.match(line.strip())
        if match:
            level = len(match.group(1))
            title = clean_text(match.group(2))
            while len(heading_stack) >= level:
                heading_stack.pop()
            heading_stack.append(title)
            section = " / ".join(heading_stack)
            blocks.append(
                Block(
                    text=title,
                    page_no=None,
                    section=section,
                    heading=title,
                    anchor=make_anchor(title, char_start=cursor, heading=title),
                    order=len(blocks),
                )
            )
            cursor += len(line) + 1
            continue
        text = clean_text(line)
        if not text:
            cursor += 1
            continue
        section = " / ".join(heading_stack) or None
        blocks.append(
            Block(
                text=text,
                page_no=None,
                section=section,
                anchor=make_anchor(text, char_start=cursor, heading=section),
                order=len(blocks),
            )
        )
        cursor += len(line) + 1

    logger.info("Markdown 解析完成", extra={"extra_fields": {"blocks": len(blocks)}})
    return blocks


def parse(path: Path, fmt: str = "txt", **kwargs: object) -> list[Block]:
    """按格式分派到 txt / md 解析。"""
    if fmt == "md":
        return parse_md(path, **kwargs)
    return parse_txt(path, **kwargs)


def page_count(path: Path, **_: object) -> int:
    """纯文本无页概念，返回 1。"""
    return 1
