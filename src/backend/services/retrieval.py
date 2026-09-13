"""混合检索：FTS5 关键词召回 + numpy 向量余弦，融合重排（架构文档 §7.2 / §14.8）。

- 关键词通道：``chunks_fts MATCH``（查询侧用 :func:`backend.utils.textutil.cjk_query`
  转逐字短语），``bm25`` 打分；
- 向量通道：查询向量与主表 ``chunks.embedding`` 暴力余弦（单机单人数万级切片 <50ms）；
- 融合：两侧分数各自 min-max 归一化后按 ``hybrid_alpha``（向量权重）加权；
- 嵌入不可用时**不报错**，自动退化为纯 FTS（``degraded=True``），保证 R-H04。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Sequence

import numpy as np

from ..db.connection import get_db
from ..utils.numpy_blob import decode_vector
from ..utils.textutil import (
    build_keyword_query,
    cjk_query,
    extract_keywords,
    required_keyword_matches,
    truncate,
)
from .embedder import get_embedder

logger = logging.getLogger(__name__)

__all__ = ["RetrievalService", "get_retrieval_service"]

# 关键词通道候选上限（先粗召回再融合）。
_FTS_CANDIDATES = 50

# 文件名去掉扩展名，用于「问题里提到某份材料」的判定。
_TITLE_EXT = re.compile(r"\.(pdf|docx?|pptx?|md|txt|html?)$", re.IGNORECASE)

# 「文档级意图」：在问**材料本身**（目录/大纲/讲了什么/安排学习），
# 而不是在问材料里的某个知识点。此类问题与正文没有词汇交集，
# 关键词与向量两条通道都会召回为空，必须走材料概览兜底。
# 注意：**绝不能**放宽到裸「是什么」这类疑问句，否则会破坏
# 「材料外问题 → 必须回『材料中未提及』」这条一票否决红线。
_DOC_INTENT = re.compile(
    r"总结|概括|概要|综述|梳理|整理|归纳|讲解|讲讲|介绍一下|介绍|分析|"
    r"安排|规划|大纲|目录|提纲|梗概|"
    r"讲了什么|讲了啥|说了什么|说了啥|主要讲|内容是|什么内容|有哪些内容|主要讲什么|"
    r"重点|要点|考点|"
    r"看一下|看一看|看看|看下|读一下|读一读|阅读|浏览|打开|"
    r"开始学习|开始吧|继续学习|继续|"
    r"这份|这篇|这个文件|该文件|这些文件|上传的"
)


def _normalize(scores: dict[str, float]) -> dict[str, float]:
    """min-max 归一化到 [0,1]；全等时统一给 1.0。"""
    if not scores:
        return {}
    values = list(scores.values())
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return {k: 1.0 for k in scores}
    return {k: (v - low) / (high - low) for k, v in scores.items()}


class RetrievalService:
    """混合检索服务（进程级单例）。"""

    _instance: "RetrievalService | None" = None

    @classmethod
    def get_instance(cls) -> "RetrievalService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 参数 ────────────────────────────────────────────
    @staticmethod
    def _params() -> tuple[int, float, float]:
        """读取检索参数（``top_k``、``hybrid_alpha``、``min_vec_score``）。"""
        db = get_db()
        rows = db.query_all(
            "SELECT key, value FROM settings WHERE key IN "
            "('retrieval.top_k','retrieval.hybrid_alpha','retrieval.min_vec_score')"
        )
        raw = {r["key"]: r["value"] for r in rows}

        def _num(key: str, default: float) -> float:
            try:
                return float(raw.get(key))
            except (TypeError, ValueError):
                return default

        return (
            int(_num("retrieval.top_k", 8)),
            _num("retrieval.hybrid_alpha", 0.5),
            _num("retrieval.min_vec_score", 0.2),
        )

    # ── 关键词通道 ──────────────────────────────────────
    def _fts_search(
        self,
        query: str,
        document_ids: Sequence[str] | None,
        collection: str | None,
    ) -> dict[str, dict[str, Any]]:
        """FTS5 召回，返回 ``{chunk_id: {row, fts_raw}}``。

        两段式召回（先精确、后兜底）：
        1. **严格短语**：整段查询按逐字短语匹配（原行为，精确优先）；
        2. 严格短语无命中时，退化为**关键词 OR + 覆盖率门限**：
           用 :func:`extract_keywords` 抽取内容词组成 OR 查询，再要求单个命中至少
           包含 ``required_keyword_matches(n)`` 个关键词，从而既恢复自然问句
           （``极限的定义是什么？``）的召回，又保证材料外问题仍召回为空。
        """
        strict = cjk_query(query)
        if strict != '""':
            result = self._fts_run(strict, document_ids, collection)
            if result:
                return result

        # ── 严格短语未命中：关键词 OR 兜底 ──────────────
        keywords = extract_keywords(query)
        if not keywords:
            return {}
        expr = build_keyword_query(keywords)
        if not expr:
            return {}
        candidates = self._fts_run(expr, document_ids, collection)
        if not candidates:
            return {}

        need = required_keyword_matches(len(keywords))
        result: dict[str, dict[str, Any]] = {}
        for cid, item in candidates.items():
            text = item["row"]["text"] or ""
            if sum(1 for kw in keywords if kw in text) >= need:
                result[cid] = item
        if not result:
            logger.info(
                "关键词兜底召回被覆盖率门限全部过滤",
                extra={"extra_fields": {"keywords": keywords, "need": need}},
            )
        return result

    def _fts_run(
        self,
        expr: str,
        document_ids: Sequence[str] | None,
        collection: str | None,
    ) -> dict[str, dict[str, Any]]:
        """执行一次 FTS5 ``MATCH`` 表达式并返回 ``{chunk_id: {row, fts_raw}}``。"""
        if expr == '""':
            return {}
        db = get_db()
        sql = (
            "SELECT c.id AS id, c.document_id AS document_id, c.page_no AS page_no, "
            "c.section AS section, c.anchor AS anchor, c.text AS text, "
            "bm25(chunks_fts) AS bm "
            "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ?"
        )
        params: list[Any] = [expr]
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            sql += f" AND c.document_id IN ({placeholders})"
            params.extend(document_ids)
        if collection:
            sql += " AND c.document_id IN (SELECT id FROM documents WHERE collection = ?)"
            params.append(collection)
        sql += " ORDER BY bm LIMIT ?"
        params.append(_FTS_CANDIDATES)

        try:
            rows = db.query_all(sql, tuple(params))
        except Exception as exc:  # noqa: BLE001 - FTS 语法异常不应中断检索
            logger.warning("FTS 召回失败：%s", type(exc).__name__)
            return {}

        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            # bm25 越小越相关（负值），取负使「越大越相关」。
            result[row["id"]] = {"row": row, "fts_raw": -float(row["bm"] or 0.0)}
        return result

    # ── 向量通道 ────────────────────────────────────────
    def _vector_search(
        self,
        query: str,
        document_ids: Sequence[str] | None,
        collection: str | None,
        min_vec_score: float,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """向量召回。

        Args:
            query: 查询文本。
            document_ids: 限定文档。
            collection: 限定分组。
            min_vec_score: 最低余弦阈值，低于该值的候选被丢弃（保证材料外问题召回为空）。

        Returns:
            ``(scores, degraded)``；``degraded=True`` 表示嵌入不可用、退化为空向量通道。
        """
        embedder = get_embedder()
        query_vec = embedder.embed_query(query)
        if query_vec.size == 0:
            return {}, True

        db = get_db()
        sql = (
            "SELECT id, document_id, page_no, section, anchor, text, embedding, embedding_dim "
            "FROM chunks WHERE embedding IS NOT NULL"
        )
        params: list[Any] = []
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            sql += f" AND document_id IN ({placeholders})"
            params.extend(document_ids)
        if collection:
            sql += " AND document_id IN (SELECT id FROM documents WHERE collection = ?)"
            params.append(collection)

        try:
            rows = db.query_all(sql, tuple(params))
        except Exception as exc:  # noqa: BLE001
            logger.warning("向量召回查询失败：%s", type(exc).__name__)
            return {}, True

        dim = int(query_vec.size)
        scores: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["embedding_dim"] is not None and int(row["embedding_dim"]) != dim:
                # 维度不一致的旧向量：跳过并告警（架构 §14.8）。
                continue
            vec = decode_vector(row["embedding"])
            if vec.size != dim:
                continue
            # 两侧均已 L2 归一化，点积即余弦。
            cos = float(np.dot(query_vec, vec))
            if cos < min_vec_score:
                # 相关度过低的向量候选直接丢弃（材料外问题 → 召回为空）。
                continue
            scores[row["id"]] = {"row": row, "vec_raw": cos}
        return scores, False

    # ── 材料概览兜底（文档级问题）────────────────────────
    @staticmethod
    def _title_variants(title: str) -> set[str]:
        """文档标题的可匹配形态：全名 + 去扩展名后的主体（≥2 字才参与匹配）。"""
        raw = (title or "").strip()
        stem = _TITLE_EXT.sub("", raw).strip()
        return {v for v in (raw, stem) if len(v) >= 2}

    def match_documents_by_title(
        self, query: str, document_ids: Sequence[str] | None = None
    ) -> list[str]:
        """返回「标题/文件名出现在问题里」的文档 id（按查询命中顺序去重）。

        用途：文件名的字面量此前**完全没有进入检索**（``chunks_fts`` 只索引正文
        ``text_seg``），所以「看下 学习好文.txt 的内容」这类问题会 0 召回，
        模型只能回答「我没有收到文件」。这里显式做一次标题匹配把这类问题认出来。
        """
        q = (query or "").strip()
        if not q:
            return []
        db = get_db()
        sql = "SELECT id, title FROM documents WHERE status = 'ready'"
        params: list[Any] = []
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            sql += f" AND id IN ({placeholders})"
            params.extend(document_ids)
        try:
            rows = db.query_all(sql, tuple(params))
        except Exception as exc:  # noqa: BLE001 - 标题匹配失败不应影响主流程
            logger.warning("标题匹配查询失败：%s", type(exc).__name__)
            return []
        matched: list[str] = []
        for row in rows:
            if any(v in q for v in self._title_variants(row["title"] or "")):
                matched.append(row["id"])
        return matched

    @staticmethod
    def is_document_directed(query: str) -> bool:
        """问题是否在问「材料本身」（目录/大纲/讲了什么/安排学习/开始学习）。"""
        return bool(_DOC_INTENT.search(query or ""))

    def material_overview(
        self,
        document_ids: Sequence[str],
        *,
        docs_limit: int = 3,
        chunks_per_doc: int = 3,
        sections_limit: int = 40,
    ) -> tuple[list[dict[str, Any]], str]:
        """构造「材料概览」：每份材料的标题 + 章节大纲 + 开头片段。

        Args:
            document_ids: 参与概览的文档 id（按序取前 ``docs_limit`` 份）。
            docs_limit: 最多覆盖几份材料。
            chunks_per_doc: 每份材料取开头几个切片。
            sections_limit: 每份材料最多列出多少章节。

        Returns:
            ``(hits, outline_text)``。

            ``hits`` 是**开头切片的真实行**（带 ``chunk_id``），因此页码/章节仍由
            :func:`build_context` 按 chunk_id 从 DB 回填，引用防线结构上不受影响；
            ``outline_text`` 是不带 ``[材料N]`` 标记的目录文本，只用于让模型了解
            材料范围，不参与引用编号。
        """
        ids = [d for d in list(document_ids)[:docs_limit] if d]
        if not ids:
            return [], ""
        db = get_db()
        placeholders = ",".join("?" for _ in ids)
        try:
            rows = db.query_all(
                "SELECT c.id, c.document_id, c.text, c.page_no, c.section, c.anchor, "
                "       d.title AS document_title "
                "FROM chunks c JOIN documents d ON d.id = c.document_id "
                f"WHERE c.document_id IN ({placeholders}) "
                "ORDER BY c.document_id, c.ordinal",
                tuple(ids),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("材料概览查询失败：%s", type(exc).__name__)
            return [], ""

        by_doc: dict[str, list[Any]] = {}
        for row in rows:
            by_doc.setdefault(row["document_id"], []).append(row)

        hits: list[dict[str, Any]] = []
        lines: list[str] = [
            "【材料概览】本轮未从材料正文检索到直接相关内容，"
            "以下为该材料的目录与开头部分，供你了解材料范围后作答。"
        ]
        for doc_id, items in by_doc.items():
            title = (items[0]["document_title"] or "未命名材料").strip()
            sections: list[str] = []
            for item in items:
                sec = (item["section"] or "").strip()
                if sec and sec not in sections:
                    sections.append(sec)
                    if len(sections) >= sections_limit:
                        break
            pages = [i["page_no"] for i in items if i["page_no"] is not None]
            head = f"● 《{title}》—— 本材料共切分为 {len(items)} 个片段"
            if pages:
                head += f"，涉及第 {min(pages)}–{max(pages)} 页"
            lines.append(head)
            if sections:
                lines.append("  章节结构：" + " / ".join(sections))
            for item in items[:chunks_per_doc]:
                anchor: dict[str, Any] = {}
                if item["anchor"]:
                    try:
                        anchor = json.loads(item["anchor"])
                    except (TypeError, ValueError):
                        anchor = {}
                hits.append({
                    "chunk_id": item["id"],
                    "document_id": doc_id,
                    "document_title": title,
                    "page_no": item["page_no"],
                    "section": item["section"],
                    "anchor": anchor,
                    "snippet": truncate(item["text"] or "", 480),
                    "score": 0.5,
                    "fts_score": None,
                    "vec_score": None,
                })
        lines.append(
            "（本概览仅用于让你了解材料范围，**不可**据此编号引用；"
            "引用编号只能指向下面给出的 [材料N] 片段。）"
        )
        return hits, "\n".join(lines)

    def material_fallback(
        self, query: str, document_ids: Sequence[str] | None
    ) -> tuple[list[dict[str, Any]], str]:
        """检索为空时的兜底：问题若指向材料本身，返回材料概览。

        触发条件（满足其一）：
        1. 问题里出现了某份材料的**标题/文件名**；
        2. 问题命中**文档级意图**（总结/大纲/讲了什么/安排学习/开始学习…）。

        并且必须存在**明确的材料作用域**（调用方已选定材料，或标题命中了某份材料）。
        这条限制是关键：否则材料外问题也会拿到概览，
        「材料未命中 → 如实回『材料中未提及』+ 零引用」这条一票否决红线就破了。

        Returns:
            ``(hits, outline)``；不适用时返回 ``([], "")``，调用方据此维持原行为。
        """
        q = (query or "").strip()
        if not q:
            return [], ""
        scoped = [d for d in (document_ids or []) if d]
        target: list[str] = []
        for doc_id in self.match_documents_by_title(q, scoped or None):
            if doc_id not in target:
                target.append(doc_id)
        if not target and scoped and self.is_document_directed(q):
            target = scoped
        if not target:
            return [], ""
        hits, outline = self.material_overview(target)
        if not hits:
            return [], ""
        logger.info(
            "材料概览兜底生效",
            extra={"extra_fields": {"docs": len(target), "chunks": len(hits)}},
        )
        return hits, outline

    # ── 主入口 ──────────────────────────────────────────
    def hybrid_search(
        self,
        query: str,
        document_ids: Sequence[str] | None = None,
        collection: str | None = None,
        top_k: int | None = None,
        mode: str = "hybrid",
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """混合检索。

        Args:
            query: 查询文本。
            document_ids: 限定文档 id；``None`` 表示全库。
            collection: 限定分组。
            top_k: 返回条数；``None`` 时取设置值。
            mode: ``hybrid | fts | vector``。

        Returns:
            ``(hits, latency_ms, degraded)``。
        """
        started = time.perf_counter()
        default_top_k, alpha, min_vec_score = self._params()
        top_k = int(top_k or default_top_k)

        query = (query or "").strip()
        if not query:
            return [], 0, False

        fts_result: dict[str, dict[str, Any]] = {}
        vec_result: dict[str, dict[str, Any]] = {}
        degraded = False

        if mode in ("hybrid", "fts"):
            fts_result = self._fts_search(query, document_ids, collection)
        if mode in ("hybrid", "vector"):
            vec_result, degraded = self._vector_search(
                query, document_ids, collection, min_vec_score
            )

        fts_scores = {cid: item["fts_raw"] for cid, item in fts_result.items()}
        vec_scores = {cid: item["vec_raw"] for cid, item in vec_result.items()}
        fts_norm = _normalize(fts_scores)
        vec_norm = _normalize(vec_scores)

        fused: dict[str, float] = {}
        all_ids = set(fts_norm) | set(vec_norm)
        for cid in all_ids:
            if mode == "fts":
                fused[cid] = fts_norm.get(cid, 0.0)
            elif mode == "vector":
                fused[cid] = vec_norm.get(cid, 0.0)
            else:
                fused[cid] = alpha * vec_norm.get(cid, 0.0) + (1 - alpha) * fts_norm.get(cid, 0.0)

        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        if not ordered:
            return [], int((time.perf_counter() - started) * 1000), degraded

        hits = self._build_hits(ordered, fts_scores, vec_scores, fts_result, vec_result)
        latency = int((time.perf_counter() - started) * 1000)
        logger.info(
            "检索完成",
            extra={"extra_fields": {"mode": mode, "hits": len(hits), "latency_ms": latency,
                                    "degraded": degraded}},
        )
        return hits, latency, degraded

    def _build_hits(
        self,
        ordered: list[tuple[str, float]],
        fts_scores: dict[str, float],
        vec_scores: dict[str, float],
        fts_result: dict[str, dict[str, Any]],
        vec_result: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """组装命中对象（补全文档标题，页码等取自 DB 行）。"""
        db = get_db()
        doc_ids = set()
        for cid, _ in ordered:
            row = (fts_result.get(cid) or vec_result.get(cid) or {}).get("row")
            if row is not None:
                doc_ids.add(row["document_id"])
        title_map: dict[str, str] = {}
        if doc_ids:
            placeholders = ",".join("?" for _ in doc_ids)
            for row in db.query_all(
                f"SELECT id, title FROM documents WHERE id IN ({placeholders})",
                tuple(doc_ids),
            ):
                title_map[row["id"]] = row["title"]

        hits: list[dict[str, Any]] = []
        for cid, score in ordered:
            source = fts_result.get(cid) or vec_result.get(cid)
            if source is None:
                continue
            row = source["row"]
            anchor: dict[str, Any] = {}
            if row["anchor"]:
                try:
                    anchor = json.loads(row["anchor"])
                except (ValueError, TypeError):
                    anchor = {}
            hits.append(
                {
                    "chunk_id": cid,
                    "document_id": row["document_id"],
                    "document_title": title_map.get(row["document_id"], ""),
                    "page_no": row["page_no"],
                    "section": row["section"],
                    "anchor": anchor,
                    "snippet": truncate(row["text"] or "", 200),
                    "score": round(float(score), 6),
                    "fts_score": round(fts_scores[cid], 6) if cid in fts_scores else None,
                    "vec_score": round(vec_scores[cid], 6) if cid in vec_scores else None,
                }
            )
        return hits


_service: RetrievalService | None = None


def get_retrieval_service() -> RetrievalService:
    """返回进程级 :class:`RetrievalService` 单例。"""
    return RetrievalService.get_instance()
