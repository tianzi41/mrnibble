"""中文文本处理工具（FTS5 检索的关键共享知识）。

════════════════════════════════════════════════════════════════════
⚠️ 最重要的一条约定：
    **入库（写入 ``chunks.text_seg`` / ``memories.content_seg``）与查询两侧，
      必须使用同一个函数 :func:`build_index_text`。**

    任何一侧偷懒用别的分词方式，都会导致「搜不到」或「乱序误命中」。
════════════════════════════════════════════════════════════════════

为什么不用 jieba 直接入库（实测结论，同环境验证过）：
- FTS5 ``tokenize='unicode61'`` 对**中文完全不切词**，搜「洛必达」命中 0；
- FTS5 ``tokenize='trigram'`` 只支持 ≥3 字符查询，搜「极限」（2 字）命中 **0**，不可用；
- jieba 对未登录词 / 专业术语 / 生僻词会切错，**不能作为唯一索引来源**；
- ✅ **逐字 CJK 索引 + unicode61 + 短语查询**：实测 2 字 / 3 字 / 中英混排全部命中，
  负例正确返回 0。

因此本模块策略：
- :func:`build_index_text` —— **逐字 CJK 索引**：中文按单字空格分隔，拉丁/数字词保持整体。
  入库与查询两侧都用它。
- :func:`cjk_query` —— 查询侧包装：把用户查询切成若干关键词，各自转成逐字索引串，
  再整体加**双引号**做短语查询（如 ``"极 限"``），避免字符乱序误命中；多关键词空格分隔
  （FTS5 隐式 AND）。
- :func:`segment` —— jieba 分词封装（优先 ``jieba.cut``），**仅用于**展示、统计等非索引场景；
  索引链路一律用 :func:`build_index_text`。
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "build_index_text",
    "cjk_query",
    "extract_keywords",
    "build_keyword_query",
    "required_keyword_matches",
    "segment",
    "clean_text",
    "truncate",
    "sha256_text",
    "contains_cjk",
    "split_keywords",
]

# CJK 统一表意文字范围：扩展 A + 基本区 + 兼容区。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# 空白字符归一（含全角空格）。
_WS = re.compile(r"[\s\u3000]+")

# 常见控制字符（保留换行 \n 与制表 \t）。
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 查询关键词抽取时丢弃的中文停顿词 / 疑问词 / 功能词（**多字**；
# 单字（的/了/是/在…）由 :func:`_is_content_token` 的长度规则统一过滤）。
_QUERY_STOPWORDS = frozenset(
    {
        "什么", "怎么", "怎样", "怎么样", "如何", "为什么", "为何", "哪些", "哪个",
        "哪里", "是不是", "是否", "多少", "几个", "什么样", "请问", "一下", "我们",
        "你们", "他们", "这个", "那个", "这些", "那些", "关于", "对于", "所以",
        "因此", "但是", "而且", "并且", "然后", "以及", "已经", "正在", "可以",
        "能够", "介绍", "说明", "解释", "讲讲", "说说", "聊聊", "简单",
    }
)

# CJK 连续段 / 拉丁数字段的提取（jieba 不可用时的兜底分词）。
_TOKEN_SPLIT = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[0-9A-Za-z]+"
)

# jieba 惰性加载状态（模块级缓存）。
_JIEBA = None
_JIEBA_CACHE_READY = False


def contains_cjk(text: str) -> bool:
    """判断字符串是否包含中日韩汉字。

    Args:
        text: 待检查文本。

    Returns:
        含汉字返回 ``True``。
    """
    return bool(text) and _CJK.search(text) is not None


def build_index_text(text: str) -> str:
    """把文本转成 FTS5 可切词的**逐字索引串**（入库与查询两侧必须用同一函数）。

    规则：
        - CJK 汉字：逐字输出，字与字之间以空格分隔（``洛必达`` → ``洛 必 达``）；
        - 拉丁字母 / 数字：连续段整体保留（``delta-3`` → ``delta`` ``3``）；
        - 其它符号（标点、空白、连字符等）：作为分隔符丢弃。

    Args:
        text: 原始文本。

    Returns:
        以空格分隔的索引串；空输入返回空串。
    """
    if not text:
        return ""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append("".join(buf))
            buf.clear()

    for ch in text:
        if _CJK.match(ch):
            flush()
            out.append(ch)
        elif ch.isalnum():
            buf.append(ch)
        else:
            flush()
    flush()
    return " ".join(out)


def _escape_phrase(token: str) -> str:
    """把单个关键词转成 FTS5 短语查询片段（``"极 限"``）。"""
    index_text = build_index_text(token)
    if not index_text:
        return ""
    # 双引号在 FTS5 短语中需转义为两个双引号。
    escaped = index_text.replace('"', '""')
    return f'"{escaped}"'


def split_keywords(query: str) -> list[str]:
    """把查询串切分为关键词列表（按空白切分，去空、去重保序）。

    Args:
        query: 用户原始查询。

    Returns:
        关键词列表。
    """
    if not query:
        return []
    parts = [p for p in _WS.split(query.strip()) if p]
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            result.append(part)
    return result


def cjk_query(query: str) -> str:
    """把用户查询转成 FTS5 ``MATCH`` 表达式（短语查询 + 隐式 AND）。

    示例::

        cjk_query("洛必达法则")   -> '"洛 必 达 法 则"'
        cjk_query("极限 delta")   -> '"极 限" "delta"'
        cjk_query("")             -> '""'   # 仍返回合法但必不命中的表达式

    Args:
        query: 用户原始查询字符串。

    Returns:
        可直接用于 ``... MATCH ?`` 的表达式字符串。
    """
    tokens = split_keywords(query)
    fragments = [frag for frag in (_escape_phrase(t) for t in tokens) if frag]
    if not fragments:
        # 无有效关键词：返回一个不可能命中的短语，保证调用方 SQL 语义统一。
        return '""'
    return " ".join(fragments)


def _is_content_token(token: str) -> bool:
    """判断分词结果是否为「内容词」（长度足够的汉字词或拉丁/数字词）。"""
    if contains_cjk(token):
        return sum(1 for ch in token if _CJK.match(ch)) >= 2
    return token.isalnum() and len(token) >= 2


def _fallback_tokens(query: str) -> list[str]:
    """jieba 不可用时的兜底切分：按 CJK 连续段 / 拉丁数字段切分。"""
    return _TOKEN_SPLIT.findall(query or "")


def extract_keywords(query: str, *, max_keywords: int = 16) -> list[str]:
    """从自然语言查询中抽取**内容关键词**（用于关键词 OR 召回兜底）。

    动机：中文问句往往没有空格（``极限的定义是什么？``），若整句作为**单个短语**
    查询，则「是什么」等疑问成分不会与材料逐字相邻 → FTS 命中为 0（漏召回）。
    本函数用 jieba 切词后丢弃停顿词/疑问词与单字，得到内容词，
    再由 :func:`build_keyword_query` 组成 OR 查询，最后配合覆盖率门限使用，
    既恢复自然问句的召回，又不牺牲「材料外 → 空召回」的精确性。

    Args:
        query: 用户原始查询。
        max_keywords: 关键词数量上限（防止超长 OR 表达式）。

    Returns:
        去重保序的内容关键词列表（可能为空）。
    """
    if not query or not query.strip():
        return []
    try:
        jieba = _get_jieba()
        tokens = [tok.strip() for tok in jieba.cut(query) if tok.strip()]
    except Exception:
        tokens = _fallback_tokens(query)

    result: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok in _QUERY_STOPWORDS or not _is_content_token(tok) or tok in seen:
            continue
        seen.add(tok)
        result.append(tok)
        if len(result) >= max_keywords:
            break
    return result


def build_keyword_query(keywords: "list[str]") -> str:
    """把关键词列表组成 FTS5 的 ``OR`` 表达式（每个关键词仍是逐字短语）。

    示例::

        build_keyword_query(["极限", "定义"])  -> '"极 限" OR "定 义"'

    Args:
        keywords: 关键词列表。

    Returns:
        可直接用于 ``... MATCH ?`` 的表达式；无有效关键词时返回空串。
    """
    fragments = [frag for frag in (_escape_phrase(k) for k in keywords) if frag]
    return " OR ".join(fragments)


def required_keyword_matches(keyword_count: int) -> int:
    """返回命中至少需要匹配的关键词数（覆盖率门限）。

    规则（在「不漏召回材料内问题」与「不误召回材料外问题」之间取平衡）：

    - ``n == 1``：全中（1）；
    - ``n == 2``：两个都中（2）——短查询信息量低，从严；
    - ``n >= 3``：**封顶 2 个**——自然问句里常混入「直接 / 告诉 / 答案」这类
      泛化词，若按 ``ceil(n/2)`` 水涨船高，会出现「问的材料内概念却 0 命中」
      的严重漏召回（实测案例：``洛必达法则怎么用？直接告诉我答案`` 5 个关键词
      只命中 2 个领域词即被过滤）。

    Args:
        keyword_count: 参与召回的关键词数量。

    Returns:
        要求命中的最少关键词数；``keyword_count <= 0`` 时返回 0。
    """
    if keyword_count <= 0:
        return 0
    if keyword_count == 1:
        return 1
    if keyword_count == 2:
        return 2
    return 2


def segment(text: str) -> str:
    """jieba 分词封装（**仅用于展示/统计，索引链路请用 build_index_text**）。

    优先使用 ``jieba.cut``，以空格连接；jieba 不可用时退化为逐字/整体切分。
    jieba 首次调用会构建词典缓存，可能略慢（约数百毫秒），属正常现象。

    Args:
        text: 原始文本。

    Returns:
        以空格分隔的分词串。
    """
    if not text:
        return ""
    try:
        jieba = _get_jieba()
        return " ".join(tok.strip() for tok in jieba.cut(text) if tok.strip())
    except Exception:
        # 兜底：退化为逐字索引串，保证「永远有可得结果」。
        return build_index_text(text)


def _get_jieba():
    """惰性加载 jieba，并把词典缓存目录重定向到数据目录（避免写 C 盘临时目录）。

    Returns:
        jieba 模块对象。
    """
    global _JIEBA, _JIEBA_CACHE_READY
    import jieba  # 延迟导入，避免无索引需求的进程加载大词典

    if not _JIEBA_CACHE_READY:
        try:
            from ..paths import data_path

            cache_dir = data_path("cache")
            cache_dir.mkdir(parents=True, exist_ok=True)
            # jieba 默认把 jieba.cache 写到系统临时目录；此处改到数据目录（Q 盘）。
            jieba.dt.tmp_dir = str(cache_dir)
        except Exception:
            pass
        _JIEBA_CACHE_READY = True
    _JIEBA = jieba
    return jieba


def clean_text(text: str) -> str:
    """清洗文本：去控制字符、归一空白（保留单换行）。

    Args:
        text: 原始文本。

    Returns:
        清洗后的文本。
    """
    if not text:
        return ""
    cleaned = _CTRL.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    # 折叠行内多空格，但不吞换行。
    cleaned = "\n".join(_WS.sub(" ", line).strip() for line in cleaned.split("\n"))
    # 折叠 3 个以上连续换行为 2 个。
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def truncate(text: str, limit: int = 200, suffix: str = "…") -> str:
    """按字符数截断文本（用于 snippet 展示）。

    Args:
        text: 原始文本。
        limit: 最大字符数（含后缀）。
        suffix: 截断后缀。

    Returns:
        截断后的文本。
    """
    if text is None:
        return ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def sha256_text(text: str) -> str:
    """计算字符串的 sha256 十六进制摘要（用于文本去重/短哈希）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
