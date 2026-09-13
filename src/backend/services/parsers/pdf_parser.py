"""PDF 解析器：pypdf 逐页解析，``page_no`` = 页码（1 起）。

扫描版 PDF（无文本层）会解析出空文本，本解析器**照常返回空列表**，
由上层 :mod:`backend.services.ingest` 置 ``status=ready`` + ``warning`` 提示
「无可提取文本层」，不抛异常（架构文档 §15/11：不做 OCR）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import Block, make_anchor
from ...utils.textutil import clean_text

logger = logging.getLogger(__name__)

__all__ = ["parse"]


def parse(path: Path, *, max_pages: int | None = None) -> list[Block]:
    """逐页解析 PDF。

    Args:
        path: PDF 文件路径。
        max_pages: 可选页数上限（调试用）。

    Returns:
        ``Block`` 列表，每页一个块（文本非空的页才产生块）。

    Raises:
        RuntimeError: pypdf 打开或读取失败。
    """
    from pypdf import PdfReader

    blocks: list[Block] = []
    order = 0
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 - 统一转运行时错误
        raise RuntimeError(f"PDF 打开失败：{type(exc).__name__}") from exc

    total = len(reader.pages)
    limit = total if max_pages is None else min(total, max_pages)

    for index in range(limit):
        page_no = index + 1
        try:
            raw = reader.pages[index].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - 单页失败不阻断整篇
            logger.warning(
                "PDF 第 %d 页解析失败", page_no,
                extra={"extra_fields": {"err": type(exc).__name__}},
            )
            raw = ""
        text = clean_text(raw)
        if not text:
            continue
        blocks.append(
            Block(
                text=text,
                page_no=page_no,
                section=None,
                anchor=make_anchor(text, slide=None),
                order=order,
            )
        )
        order += 1

    logger.info(
        "PDF 解析完成",
        extra={"extra_fields": {"pages": total, "parsed_pages": len(blocks)}},
    )
    return blocks


def page_count(path: Path) -> int:
    """返回 PDF 总页数（供 ``documents.page_count``）。"""
    from pypdf import PdfReader

    try:
        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001
        return 0
