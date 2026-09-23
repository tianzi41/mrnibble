"""长期记忆服务（架构文档 §4.2 / §10 —— 一票否决项：删除后不可再召回）。

**结构性保证（非提示词约束）**：

1. 删除 = 物理删除整行（``DELETE FROM memories``），``embedding`` BLOB 随行消失；
   触发器 ``trg_mem_ad`` 同步从 ``memories_fts`` 删除索引行；
2. 召回**只有**一个入口 :meth:`MemoryService.recall`：
   - 关键词召回：``memories_fts MATCH ... JOIN memories 主表``，已删行在主表不存在
     → JOIN 无结果 → 结构上不可能被召回；
   - 向量召回：``SELECT ... FROM memories``（读主表），行已删 → 不在候选；
3. **禁止任何其它模块直接查询 ``memories_fts``**（本文件是唯一入口）。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any, Sequence

import numpy as np

from ..db.connection import get_db
from ..models.retrieval import Hit  # noqa: F401  （仅类型语义复用）
from ..utils.ids import new_id
from ..utils.numpy_blob import decode_vector, encode_vector, l2_normalize
from ..utils.textutil import build_index_text, cjk_query, truncate
from ..utils.timeutil import now_iso
from .embedder import get_embedder

logger = logging.getLogger(__name__)

__all__ = ["MemoryService", "get_memory_service"]

# 记忆类型白名单（§4.2）。
MEMORY_TYPES = ("preference", "progress", "knowledge_gap", "fact")

# 召回上限与最低相似度。
_RECALL_LIMIT = 6
_MIN_RECALL_SCORE = 0.15


def _seg(text: str) -> str:
    """生成 FTS 索引文本（入库与查询两侧共用同一函数，见 §14）。"""
    return build_index_text(text)


class MemoryService:
    """长期记忆 CRUD 与召回（进程级单例）。"""

    _instance: "MemoryService | None" = None

    @classmethod
    def get_instance(cls) -> "MemoryService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 内部工具 ────────────────────────────────────────
    def _embed(self, content: str) -> tuple[bytes | None, int | None, str | None]:
        """为记忆生成嵌入向量（失败返回 ``(None,None,None)``，不阻断写入）。"""
        try:
            vectors, dim, model = get_embedder().embed([content])
            if vectors.shape[0] == 0:
                return None, None, None
            return encode_vector(l2_normalize(vectors[0])), int(dim), str(model)
        except Exception:  # pragma: no cover - 嵌入失败不阻断记忆写入
            logger.warning("记忆嵌入失败，已退化为无向量", exc_info=True)
            return None, None, None

    @staticmethod
    def _to_out(row) -> dict[str, Any]:
        """行 → 对外字典（§6.7 Memory）。"""
        return {
            "id": row["id"],
            "type": row["type"],
            "content": row["content"],
            "source": row["source"],
            "confidence": float(row["confidence"] or 0.5),
            "document_id": row["document_id"],
            "conversation_id": row["conversation_id"],
            "recall_count": int(row["recall_count"] or 0),
            "last_referenced_at": row["last_referenced_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ── CRUD ────────────────────────────────────────────
    def add(
        self,
        content: str,
        *,
        type_: str = "fact",
        source: str = "manual",
        confidence: float = 0.5,
        document_id: str | None = None,
        conversation_id: str | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        """新增一条记忆并写入 FTS 与向量索引。

        Raises:
            AppError: 1000 内容为空或类型非法。
        """
        content = (content or "").strip()
        if not content:
            from ..errors import AppError

            raise AppError(1000, "记忆内容不能为空")
        if type_ not in MEMORY_TYPES:
            from ..errors import AppError

            raise AppError(1000, None, f"记忆类型必须是 {'/'.join(MEMORY_TYPES)}")

        emb, dim, model = self._embed(content)
        ts = now_iso()
        mid = new_id()
        db = get_db()
        db.execute(
            "INSERT INTO memories(id, type, content, content_seg, source, confidence,"
            " document_id, conversation_id, collection, embedding, embedding_dim,"
            " embedding_model, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mid, type_, content, _seg(content), source, float(confidence),
                document_id, conversation_id, collection, emb, dim, model, ts, ts,
            ),
        )
        row = db.query_one("SELECT * FROM memories WHERE id = ?", (mid,))
        assert row is not None
        return self._to_out(row)

    def list(
        self,
        *,
        type_: str | None = None,
        collection: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页列出记忆（主表为唯一真源）。"""
        db = get_db()
        where, params = "1=1", []
        if type_:
            where += " AND type = ?"
            params.append(type_)
        if collection:
            where += " AND collection = ?"
            params.append(collection)
        total = db.query_one(
            f"SELECT COUNT(*) AS n FROM memories WHERE {where}", tuple(params)
        )["n"]
        rows = db.query_all(
            f"SELECT * FROM memories WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        )
        return [self._to_out(r) for r in rows], int(total)

    def update(self, mid: str, *, content: str | None = None, type_: str | None = None) -> dict[str, Any]:
        """编辑记忆（内容变化时同步重建索引文本与向量）。"""
        db = get_db()
        row = db.query_one("SELECT * FROM memories WHERE id = ?", (mid,))
        if row is None:
            from ..errors import AppError

            raise AppError(1001, "记忆不存在")
        new_content = (content if content is not None else row["content"]).strip()
        new_type = type_ or row["type"]
        if type_ and type_ not in MEMORY_TYPES:
            from ..errors import AppError

            raise AppError(1000, None, f"记忆类型必须是 {'/'.join(MEMORY_TYPES)}")
        emb, dim, model = self._embed(new_content)
        db.execute(
            "UPDATE memories SET content = ?, content_seg = ?, type = ?, embedding = ?,"
            " embedding_dim = ?, embedding_model = ?, updated_at = ? WHERE id = ?",
            (new_content, _seg(new_content), new_type, emb, dim, model, now_iso(), mid),
        )
        row = db.query_one("SELECT * FROM memories WHERE id = ?", (mid,))
        assert row is not None
        return self._to_out(row)

    def delete(self, mid: str) -> bool:
        """**硬删除**单条记忆（物理删行，触发器同步清 FTS 索引）。"""
        db = get_db()
        cur = db.execute("DELETE FROM memories WHERE id = ?", (mid,))
        return cur.rowcount > 0

    def bulk_delete(self, ids: Sequence[str]) -> int:
        """批量硬删除。"""
        if not ids:
            return 0
        db = get_db()
        total = 0
        with db.transaction():
            for mid in ids:
                total += db.execute("DELETE FROM memories WHERE id = ?", (mid,)).rowcount
        return total

    def clear(self) -> int:
        """清空全部记忆（物理删除）。"""
        db = get_db()
        n = db.query_one("SELECT COUNT(*) AS n FROM memories")["n"]
        db.execute("DELETE FROM memories")
        return int(n)

    # ── 召回（唯一入口）────────────────────────────────
    def recall(
        self,
        query: str,
        *,
        limit: int = _RECALL_LIMIT,
        document_id: str | None = None,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        """按查询召回相关记忆。

        两条通道都以 ``memories`` 主表为真源：
        - 关键词：``memories_fts MATCH`` → ``JOIN memories``（已删行不可能 JOIN 上）；
        - 向量：``SELECT embedding FROM memories``（已删行不在候选集）。

        Returns:
            记忆字典列表（已按相关度排序）；不可用时返回空列表而非报错。
        """
        query = (query or "").strip()
        if not query:
            return []
        db = get_db()
        where, params = "1=1", []
        if document_id:
            where += " AND m.document_id = ?"
            params.append(document_id)
        if collection:
            where += " AND m.collection = ?"
            params.append(collection)

        scores: dict[int, float] = {}

        # 通道 1：FTS 关键词（JOIN 主表 —— 结构上杜绝召回已删除行）
        phrase = cjk_query(query)
        if phrase != '""':
            try:
                rows = db.query_all(
                    "SELECT m.rowid AS rowid FROM memories_fts f"
                    " JOIN memories m ON m.rowid = f.rowid"
                    f" WHERE memories_fts MATCH ? AND {where}"
                    " ORDER BY bm25(memories_fts) LIMIT ?",
                    (phrase, *params, limit * 3),
                )
                for r in rows:
                    scores[r["rowid"]] = max(scores.get(r["rowid"], 0.0), 1.0)
            except Exception:  # pragma: no cover - FTS 异常不阻断
                logger.warning("记忆 FTS 召回失败", exc_info=True)

        # 通道 2：向量余弦（读主表）
        try:
            qvec = get_embedder().embed_query(query)
            if qvec is not None and np.size(qvec):
                qv = l2_normalize(np.asarray(qvec, dtype=np.float32))
                rows = db.query_all(
                    f"SELECT rowid, embedding FROM memories WHERE embedding IS NOT NULL"
                    f" AND ({where.replace('m.', '')})",
                    tuple(params),
                )
                for r in rows:
                    vec = decode_vector(r["embedding"])
                    if vec.size == 0 or vec.size != qv.size:
                        continue
                    denom = float(np.linalg.norm(vec)) or 1.0
                    sim = float(np.dot(qv, vec) / denom)
                    if sim >= _MIN_RECALL_SCORE:
                        scores[r["rowid"]] = max(scores.get(r["rowid"], 0.0), sim)
        except Exception:  # pragma: no cover
            logger.warning("记忆向量召回失败", exc_info=True)

        if not scores:
            return []
        top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        out: list[dict[str, Any]] = []
        for rowid, score in top:
            row = db.query_one(
                f"SELECT * FROM memories WHERE rowid = ? AND {where.replace('m.', '')}",
                (rowid, *params),
            )
            if row is None:
                continue  # 已被删除 → 永不召回（红线 #5）
            item = self._to_out(row)
            item["score"] = round(score, 4)
            out.append(item)
            self._touch(rowid)
        return out

    def _touch(self, rowid: int) -> None:
        """更新召回计数与最近引用时间（失败不影响主流程）。"""
        try:
            get_db().execute(
                "UPDATE memories SET recall_count = recall_count + 1,"
                " last_referenced_at = ? WHERE rowid = ?",
                (now_iso(), rowid),
            )
        except Exception:  # pragma: no cover
            logger.debug("记忆计数更新失败", exc_info=True)

    # ── 导出 ────────────────────────────────────────────
    def export(self, fmt: str = "json") -> tuple[str, str, str]:
        """导出全部现存记忆。

        Returns:
            ``(content, filename, mimetype)``。
        """
        items, _ = self.list(page=1, page_size=100000)
        stamp = now_iso()[:10]
        if fmt == "md":
            lines = ["# 啃书先生 · 长期记忆导出", ""]
            for it in items:
                lines.append(f"- **[{it['type']}]** {it['content']}  ")
                lines.append(f"  （{it['created_at']}，召回 {it['recall_count']} 次）")
            return "\n".join(lines), f"mrnibble-memories-{stamp}.md", "text/markdown; charset=utf-8"
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["id", "type", "content", "source", "confidence", "created_at", "recall_count"])
            for it in items:
                writer.writerow([
                    it["id"], it["type"], it["content"], it["source"],
                    it["confidence"], it["created_at"], it["recall_count"],
                ])
            return buf.getvalue(), f"mrnibble-memories-{stamp}.csv", "text/csv; charset=utf-8"
        return (
            json.dumps(items, ensure_ascii=False, indent=2),
            f"mrnibble-memories-{stamp}.json",
            "application/json; charset=utf-8",
        )


def get_memory_service() -> MemoryService:
    """返回记忆服务单例。"""
    return MemoryService.get_instance()
