"""切片器：把 ``Block`` 列表切成保留页码/章节的检索单元。

设计要点（保证引用页码真实）：
- **绝不让一个切片跨越页边界**：``(page_no, section)`` 变化即强制切分；
  这样每个切片只归属一个页码，引用回填的 ``page_no`` 必然等于该片段真实所在页。
- 长块按 ``max_chars`` 切分，并在句末标点处优先断开；相邻切片带 ``overlap_chars`` 重叠，
  避免答案正好落在切口处。
- 提供**生成器** :func:`iter_chunks`，配合上层分批（batch=32）流式落库，
  避免 1000 页 PDF 在内存中同时持有全部向量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from ..utils.textutil import build_index_text, clean_text
from .parsers.base import Block, make_anchor

__all__ = ["ChunkConfig", "ChunkData", "iter_chunks", "chunk_blocks"]


@dataclass(slots=True)
class ChunkConfig:
    """切片参数。

    Attributes:
        max_chars: 单切片最大字符数。
        min_chars: 单切片最小字符数（过短会与前一片合并，切分时仅作参考）。
        overlap_chars: 相邻切片重叠字符数。
    """

    max_chars: int = 800
    min_chars: int = 120
    overlap_chars: int = 120


@dataclass(slots=True)
class ChunkData:
    """切片数据（尚未落库）。

    Attributes:
        text: 切片原文。
        text_seg: 逐字 CJK 索引串（写入 ``chunks.text_seg``，供 FTS）。
        page_no: 页码/幻灯片序号。
        section: 章节/标题路径。
        anchor: 定位信息。
        token_count: 索引词元个数（近似）。
    """

    text: str
    text_seg: str
    page_no: int | None
    section: str | None
    anchor: dict[str, Any] = field(default_factory=dict)
    token_count: int = 0


_SENTENCE_ENDINGS = "。！？；\n.!?;"


def _split_text(text: str, max_chars: int) -> list[str]:
    """把一段文本按 ``max_chars`` 切分，优先在句末标点断开。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            window = text[start:end]
            cut = max(window.rfind(ch) for ch in _SENTENCE_ENDINGS)
            if cut > int(max_chars * 0.5):
                end = start + cut + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        start = end
    return pieces


def _make_chunk(
    text: str,
    page_no: int | None,
    section: str | None,
    heading: str | None,
    char_start: int,
    cfg: ChunkConfig,
) -> ChunkData:
    """构造一个 :class:`ChunkData`。"""
    body = clean_text(text)
    seg = build_index_text(body)
    anchor = make_anchor(body, char_start=char_start, slide=page_no if page_no else None, heading=heading or section)
    return ChunkData(
        text=body,
        text_seg=seg,
        page_no=page_no,
        section=section,
        anchor=anchor,
        token_count=len(seg.split()) if seg else 0,
    )


def _tail(text: str, overlap: int) -> str:
    """取尾部重叠片段（按句界回退，避免把词切一半）。"""
    if overlap <= 0 or not text:
        return ""
    tail = text[-overlap:]
    for idx, ch in enumerate(tail):
        if ch in _SENTENCE_ENDINGS or ch in "，,、 ":
            return tail[idx + 1 :].strip()
    return tail.strip()


def iter_chunks(blocks: Sequence[Block], cfg: ChunkConfig | None = None) -> Iterator[ChunkData]:
    """把 ``Block`` 序列切分为切片（生成器，逐块产出，内存占用低）。

    Args:
        blocks: 解析得到的块序列。
        cfg: 切片参数。

    Yields:
        :class:`ChunkData`。
    """
    cfg = cfg or ChunkConfig()
    buf: list[str] = []
    buf_len = 0
    cur_key: tuple[int | None, str | None] | None = None
    cur_page: int | None = None
    cur_section: str | None = None
    cur_heading: str | None = None
    anchor_start = 0

    def flush_text() -> str:
        return "".join(buf).strip()

    for block in blocks:
        key = (block.page_no, block.section)
        # 页/章节变化 → 强制切分（保证切片不跨页）。
        if buf and key != cur_key:
            chunk = _make_chunk(flush_text(), cur_page, cur_section, cur_heading, anchor_start, cfg)
            if chunk.text:
                yield chunk
            tail = _tail(chunk.text, cfg.overlap_chars)
            buf = [tail] if tail else []
            buf_len = len(tail)
            anchor_start = 0

        cur_key = key
        cur_page = block.page_no
        cur_section = block.section
        cur_heading = block.heading or (block.anchor or {}).get("heading")

        for piece in _split_text(block.text, cfg.max_chars):
            if buf and buf_len + len(piece) + 1 > cfg.max_chars:
                chunk = _make_chunk(flush_text(), cur_page, cur_section, cur_heading, anchor_start, cfg)
                if chunk.text:
                    yield chunk
                tail = _tail(chunk.text, cfg.overlap_chars)
                buf = [tail] if tail else []
                buf_len = len(tail)
                anchor_start = 0
            if not buf:
                anchor_start = int((block.anchor or {}).get("char_start", 0) or 0)
            buf.append(piece)
            buf_len += len(piece) + 1

    if buf:
        chunk = _make_chunk(flush_text(), cur_page, cur_section, cur_heading, anchor_start, cfg)
        if chunk.text:
            yield chunk


def chunk_blocks(blocks: Sequence[Block], cfg: ChunkConfig | None = None) -> list[ChunkData]:
    """切分并返回列表（小文档/测试用；大文档请用 :func:`iter_chunks`）。"""
    return list(iter_chunks(blocks, cfg))
