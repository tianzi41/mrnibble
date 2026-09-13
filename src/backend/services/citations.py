"""引用解析与回填（架构文档 §9 —— 一票否决项：引用页码不可伪造）。

**核心原则（结构保证，非提示词约束）**：

1. 模型的*唯一*引用通道是索引号 ``[[c:N]]``，且 ``N`` 必须命中本轮检索注入的引用表；
2. ``page_no / section / anchor / document_title`` 一律由服务端按 ``chunk_id``
   从 :class:`backend.models.retrieval.Hit`（即 ``chunks`` 表）回填；
3. 模型在结构化输出中**不存在**任何可写页码的字段，正文中出现的其它数字
   也不会被系统当作页码；
4. ``N`` 越界 / 未命中 → 该角标被**整体丢弃**并记 ``events``，不产生引用对象。

因此「引用页码来自模型编造」在结构上不可能发生。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from ..models.retrieval import Hit
from .events import log_event

logger = logging.getLogger(__name__)

__all__ = [
    "CITATION_MARK",
    "CONCLUSION_PATTERNS",
    "Citation",
    "NOT_MENTIONED_PHRASE",
    "build_context",
    "resolve_citations",
    "has_conclusion_pattern",
]

# 模型输出引用的唯一合法形态：[[c:1]]（允许中括号内出现空格）。
CITATION_MARK = re.compile(r"\[\[\s*c\s*:\s*(\d+)\s*\]\]")

# 「材料外」兜底措辞： hits 为空时强制输出，产品层据此校验。
NOT_MENTIONED_PHRASE = "材料中未提及"

# 结论句式（用于材料边界与护栏的*辅助*语义判定；主防线是结构化字段）。
# **唯一定义处**：guardrails 与 dev 自检脚本一律从这里导入，避免两份正则漂移。
CONCLUSION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"答案是"),
    re.compile(r"正确答案是"),
    re.compile(r"正确答案为"),
    re.compile(r"结果是"),
    re.compile(r"答案为"),
    re.compile(r"最终答案"),
    re.compile(r"综上所述[，,]?答案"),
    re.compile(r"the answer is", re.IGNORECASE),
    re.compile(r"therefore the answer", re.IGNORECASE),
)

# 单条引用展示的片段长度上限（避免把整页原文回传前端/存库）。
_SNIPPET_LIMIT = 220


def has_conclusion_pattern(text: str) -> bool:
    """文本是否命中「结论句式」正则（护栏与材料边界的辅助判定）。"""
    return any(p.search(text or "") for p in CONCLUSION_PATTERNS)


def build_context(
    hits: Sequence[Hit | dict[str, Any]], *, outline: str = ""
) -> tuple[str, dict[int, Hit]]:
    """把检索命中渲染为注入提示词的编号上下文，并生成引用表。

    Args:
        hits: 服务端检索结果（``page_no/section`` 已由 DB 回填）。
              兼容 :class:`Hit` 对象与等价 dict（检索服务两种形态都可能返回）。
        outline: 可选的「材料概览」文本（标题 + 章节大纲），置于编号材料之前。
                 它**不带** ``[材料N]`` 标记，因此不参与引用编号，只用于让模型
                 了解材料范围（对应文档级问题，如「总结一下这份资料」）。

    Returns:
        ``(context_text, table)``；``table`` 把编号映射回 :class:`Hit`，
        是本轮回答页码的*唯一*权威来源。
    """
    table: dict[int, Hit] = {}
    parts: list[str] = []
    for i, item in enumerate(hits, start=1):
        hit = Hit.model_validate(item) if isinstance(item, dict) else item
        table[i] = hit
        loc = _loc_label(hit)
        parts.append(f"[材料{i}]{loc}\n{hit.snippet}")
    if not parts:
        return "", {}
    body = "\n\n".join(parts)
    return (f"{outline}\n\n{body}" if outline else body), table


def _loc_label(hit: Hit) -> str:
    """命中片段的位置标签（页码/章节，全部来自 DB）。"""
    bits: list[str] = []
    if hit.page_no is not None:
        bits.append(f"第 {hit.page_no} 页")
    if hit.section:
        bits.append(hit.section)
    return f"《{hit.document_title}》{' · '.join(bits)}" if bits else f"《{hit.document_title}》"


def resolve_citations(
    text: str,
    table: dict[int, Hit],
    *,
    conversation_id: str | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    """把回答中的 ``[[c:N]]`` 替换为展示角标 ``[N]`` 并回填权威页码。

    Args:
        text: 模型原始输出（Markdown）。
        table: 本轮引用表（来自 :func:`build_context`）。
        conversation_id: 可选，用于事件日志关联。

    Returns:
        ``(display_text, citations, dropped)``；
        ``dropped`` 为越界/未命中而被丢弃的角标数量。

    结构保证:
        返回的每个 ``citation`` 的 ``page_no / section / anchor / document_title``
        都取自 ``table[N]``（即 ``chunks`` 表），模型没有任何写入通道。
    """
    citations: list[dict[str, Any]] = []
    seen: set[int] = set()
    dropped = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal dropped
        n = int(m.group(1))
        hit = table.get(n)
        if hit is None:
            dropped += 1
            log_event("citation_dropped", {
                "conversation_id": conversation_id, "index": n,
                "table_size": len(table),
            })
            return ""  # 越界角标整体剔除，绝不生成伪造页码
        if n in seen:
            return f"[{n}]"  # 重复引用同一片段：只展示，不重复入表
        seen.add(n)
        citations.append({
            "n": n,
            "chunk_id": hit.chunk_id,
            "document_id": hit.document_id,
            "document_title": hit.document_title,
            "page_no": hit.page_no,
            "section": hit.section,
            "anchor": hit.anchor,
            "snippet": (hit.snippet or "")[:_SNIPPET_LIMIT],
        })
        return f"[{n}]"

    display = CITATION_MARK.sub(_sub, text or "")
    # 清理剔除角标后可能残留的多余空格/空行。
    display = re.sub(r"[ \t]{2,}", " ", display)
    display = re.sub(r"\n{3,}", "\n\n", display).strip()
    citations.sort(key=lambda c: c["n"])
    return display, citations, dropped
