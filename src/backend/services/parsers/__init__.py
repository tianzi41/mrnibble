"""解析器注册表：按格式分派到具体解析器。

统一入口：
- :func:`detect_format` —— 由文件名/URL 推断格式；
- :func:`parse` —— 解析文件为 ``Block`` 列表；
- :func:`count_pages` —— 返回页数/幻灯片数（用于 ``documents.page_count``）。

支持的格式：``pdf | docx | pptx | md | txt | html``（至少 5 种，满足八件套#1）。
"""

from __future__ import annotations

from pathlib import Path

from .base import Block
from . import (
    docx_parser,
    html_parser,
    pdf_parser,
    pptx_parser,
    text_parser,
)

__all__ = [
    "SUPPORTED_FORMATS",
    "EXT_TO_FMT",
    "detect_format",
    "is_supported",
    "parse",
    "count_pages",
    "Block",
]

SUPPORTED_FORMATS: frozenset[str] = frozenset({"pdf", "docx", "pptx", "md", "txt", "html"})

EXT_TO_FMT: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".text": "txt",
    ".html": "html",
    ".htm": "html",
}


def detect_format(filename: str) -> str | None:
    """由文件名推断格式。

    Args:
        filename: 文件名或 URL。

    Returns:
        小写格式名；无法识别时返回 ``None``。
    """
    suffix = Path(filename).suffix.lower()
    return EXT_TO_FMT.get(suffix)


def is_supported(fmt: str | None) -> bool:
    """判断格式是否受支持。"""
    return bool(fmt) and fmt in SUPPORTED_FORMATS


def parse(path: Path, fmt: str, **kwargs: object) -> list[Block]:
    """解析文件为 ``Block`` 列表。

    Args:
        path: 文件路径。
        fmt: 格式名（见 :data:`SUPPORTED_FORMATS`）。
        **kwargs: 传给具体解析器的可选参数（如 html 的 ``url``）。

    Returns:
        ``Block`` 列表。

    Raises:
        ValueError: 不支持的格式。
        RuntimeError: 解析失败。
    """
    if fmt == "pdf":
        return pdf_parser.parse(path)
    if fmt == "docx":
        return docx_parser.parse(path)
    if fmt == "pptx":
        return pptx_parser.parse(path)
    if fmt in ("md", "txt"):
        return text_parser.parse(path, fmt)
    if fmt == "html":
        return html_parser.parse(path, **kwargs)
    raise ValueError(f"不支持的格式：{fmt}")


def count_pages(path: Path, fmt: str) -> int:
    """返回页数/幻灯片数（无页概念时返回 0/1）。"""
    try:
        if fmt == "pdf":
            return pdf_parser.page_count(path)
        if fmt == "pptx":
            return pptx_parser.page_count(path)
        if fmt == "docx":
            return docx_parser.page_count(path)
        if fmt in ("md", "txt"):
            return text_parser.page_count(path)
        if fmt == "html":
            return html_parser.page_count(path)
    except Exception:  # noqa: BLE001 - 页数统计失败不影响主流程
        return 0
    return 0
