"""网页解析器：BeautifulSoup 提取正文与标题层级（HTML 入库为 ``html`` 格式）。"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import Block, make_anchor
from ...utils.textutil import clean_text

logger = logging.getLogger(__name__)

__all__ = ["parse", "parse_html", "extract_title"]

_STRIP_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "aside", "form")


def _read_html(path: Path) -> str:
    """读取 HTML 文件（UTF-8 回退 GB18030）。"""
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def extract_title(html: str, fallback: str = "网页") -> str:
    """提取网页标题（``<title>`` 或首个 ``<h1>``）。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    if soup.title and soup.title.string:
        title = clean_text(soup.title.string)
        if title:
            return title
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))
        if title:
            return title
    return fallback


def parse_html(html: str, *, url: str | None = None) -> list[Block]:
    """把 HTML 文本解析为 ``Block`` 列表。

    Args:
        html: HTML 源码。
        url: 来源 URL（仅用于日志/元数据）。

    Returns:
        ``Block`` 列表（标题块更新 section，正文块按段落）。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag_name in _STRIP_TAGS:
        for node in soup.find_all(tag_name):
            node.decompose()

    root = soup.body or soup
    blocks: list[Block] = []
    heading_stack: list[str] = []
    cursor = 0
    current_page = 1

    for element in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "td"]):
        text = clean_text(element.get_text(" ", strip=True))
        if not text:
            continue
        tag = element.name.lower()
        if tag.startswith("h") and tag[1:].isdigit():
            level = int(tag[1:])
            while len(heading_stack) >= level:
                heading_stack.pop()
            heading_stack.append(text)
            section = " / ".join(heading_stack)
            blocks.append(
                Block(
                    text=text,
                    page_no=None,
                    section=section,
                    heading=text,
                    anchor=make_anchor(text, char_start=cursor, heading=text),
                    order=len(blocks),
                )
            )
        else:
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
        cursor += len(text) + 1

    logger.info(
        "HTML 解析完成",
        extra={"extra_fields": {"blocks": len(blocks), "url": bool(url)}},
    )
    return blocks


def parse(path: Path, **kwargs: object) -> list[Block]:
    """从 HTML 文件解析。"""
    url = kwargs.get("url")
    return parse_html(_read_html(path), url=url if isinstance(url, str) else None)


def page_count(path: Path, **_: object) -> int:
    """网页无页概念，返回 1。"""
    return 1
