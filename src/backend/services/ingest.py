"""文档入库编排：上传 → 解析 → 切片 → 嵌入 → 落库（架构文档 §7.1）。

要点：
- 上传即返回（``status=pending``），解析在后台任务执行；
- 解析失败置 ``status=failed`` + ``error``；扫描版 PDF（无文本层）置
  ``status=ready`` + ``warning``（不做 OCR，架构 §15/11）；
- 切片**分批 batch=32 流式落库**，控制长文档内存峰值（架构 §15/9）；
- 删除文档依赖 ``ON DELETE CASCADE`` 级联删 ``chunks``，触发器同步删 FTS。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Iterator

from ..config import get_config
from ..db.connection import get_db
from ..errors import AppError
from ..utils.ids import new_id
from ..utils.timeutil import now_iso
from . import parsers
from .chunker import ChunkConfig, ChunkData, iter_chunks
from .parsers.base import Block

logger = logging.getLogger(__name__)

__all__ = ["IngestService", "get_ingest_service", "NO_TEXT_WARNING"]

# 无文本层提示（扫描版/纯图片 PDF，不做 OCR）。
NO_TEXT_WARNING = "该文件无可提取文本层（可能是扫描版或纯图片 PDF），无法检索"

# 落库批大小（嵌入与插入按批进行）。
BATCH_SIZE = 32


def _sha256_bytes(data: bytes) -> str:
    """返回字节串的 sha256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def row_to_document(row: Any) -> dict[str, Any]:
    """把 ``documents`` 行转换为对外 ``Document`` 字典。"""
    import json

    tags: list[str] = []
    raw_tags = row["tags"]
    if raw_tags:
        try:
            parsed = json.loads(raw_tags)
            if isinstance(parsed, list):
                tags = [str(t) for t in parsed]
        except (ValueError, TypeError):
            tags = []
    return {
        "id": row["id"],
        "title": row["title"],
        "source_type": row["source_type"],
        "fmt": row["fmt"],
        "page_count": int(row["page_count"] or 0),
        "status": row["status"],
        "error": row["error"],
        "warning": row["warning"] if "warning" in row.keys() else None,
        "collection": row["collection"],
        "tags": tags,
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class IngestService:
    """文档入库服务（进程级单例）。"""

    _instance: "IngestService | None" = None

    @classmethod
    def get_instance(cls) -> "IngestService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 创建 ────────────────────────────────────────────
    def create_from_bytes(
        self,
        filename: str,
        data: bytes,
        collection: str | None = None,
    ) -> tuple[str | None, str | None]:
        """保存上传文件并创建 ``documents`` 记录（不解析）。

        Args:
            filename: 原始文件名。
            data: 文件字节。
            collection: 可选分组标签。

        Returns:
            ``(doc_id, skip_reason)``：成功时 ``doc_id`` 非空、``skip_reason`` 为 ``None``；
            被跳过时 ``doc_id`` 为 ``None``。
        """
        db = get_db()
        cfg = get_config()
        fmt = parsers.detect_format(filename)
        if not parsers.is_supported(fmt):
            return None, f"不支持的格式：{Path(filename).suffix or filename}"

        max_bytes = cfg.max_upload_mb * 1024 * 1024
        if len(data) > max_bytes:
            return None, f"文件过大（>{cfg.max_upload_mb}MB）"
        if not data:
            return None, "空文件"

        digest = _sha256_bytes(data)
        existing = db.query_one("SELECT id, title FROM documents WHERE file_hash = ?", (digest,))
        if existing is not None:
            return None, f"重复文件：已存在于资料库（{existing['title']}）"

        doc_id = new_id()
        ext = Path(filename).suffix.lower()
        rel_path = f"{doc_id}{ext}"
        target = cfg.files_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

        ts = now_iso()
        db.execute(
            "INSERT INTO documents(id, title, source_type, fmt, original_path, file_hash, "
            "size_bytes, page_count, status, collection, tags, created_at, updated_at) "
            "VALUES (?, ?, 'file', ?, ?, ?, ?, 0, 'pending', ?, ?, ?, ?)",
            (doc_id, Path(filename).name, fmt, rel_path, digest, len(data), collection,
             "[]", ts, ts),
        )
        logger.info(
            "已登记上传文档",
            extra={"extra_fields": {"doc_id": doc_id, "fmt": fmt, "size": len(data)}},
        )
        return doc_id, None

    def create_from_url(self, url: str, collection: str | None = None) -> str:
        """抓取网页正文并登记为 ``html`` 文档。

        Args:
            url: 网页地址。
            collection: 可选分组。

        Returns:
            新建文档 id。

        Raises:
            AppError: ``3003`` 抓取失败；``3000`` 非 http(s)。
        """
        import httpx

        if not url.lower().startswith(("http://", "https://")):
            raise AppError(3000, "仅支持 http/https 网页地址")
        try:
            with make_client(url, timeout=20.0, follow_redirects=True) as client:
                resp = client.get(url, headers={"User-Agent": "ZhiBan/1.0"})
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:  # noqa: BLE001
            raise AppError(3003, "网页抓取失败", f"{type(exc).__name__}") from exc

        title = parsers.html_parser.extract_title(html, fallback=url)
        db = get_db()
        cfg = get_config()
        doc_id = new_id()
        rel_path = f"{doc_id}.html"
        (cfg.files_dir / rel_path).write_text(html, encoding="utf-8")
        digest = _sha256_bytes(html.encode("utf-8"))
        ts = now_iso()
        db.execute(
            "INSERT INTO documents(id, title, source_type, fmt, original_path, source_url, "
            "file_hash, size_bytes, page_count, status, collection, tags, created_at, updated_at) "
            "VALUES (?, ?, 'url', 'html', ?, ?, ?, ?, 0, 'pending', ?, '[]', ?, ?)",
            (doc_id, title, rel_path, url, digest, len(html.encode("utf-8")), collection, ts, ts),
        )
        return doc_id

    # ── 解析 ────────────────────────────────────────────
    def parse_document(self, doc_id: str) -> None:
        """解析文档并落库切片（供后台任务调用）。

        Args:
            doc_id: 文档 id。不存在时静默返回（避免后台任务抛错）。
        """
        db = get_db()
        cfg = get_config()
        row = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        if row is None:
            logger.warning("解析任务：文档不存在 %s", doc_id)
            return

        fmt = row["fmt"]
        rel_path = row["original_path"]
        path = cfg.files_dir / rel_path if rel_path else None
        db.execute(
            "UPDATE documents SET status='parsing', error=NULL, warning=NULL, updated_at=? WHERE id=?",
            (now_iso(), doc_id),
        )

        try:
            if path is None or not path.exists():
                raise RuntimeError("原始文件缺失")
            kwargs: dict[str, Any] = {}
            if fmt == "html":
                kwargs["url"] = row["source_url"]
            blocks: list[Block] = parsers.parse(path, fmt, **kwargs)
            page_count = parsers.count_pages(path, fmt)
            total_chars = sum(len(b.text) for b in blocks)

            # 重新解析：先清空旧切片（级联触发器同步 FTS）。
            db.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))

            embedder = _get_embedder()
            inserted = self._insert_chunks(db, doc_id, iter_chunks(blocks, ChunkConfig()), embedder)

            warning = NO_TEXT_WARNING if total_chars == 0 else None
            db.execute(
                "UPDATE documents SET status='ready', page_count=?, warning=?, error=NULL, "
                "updated_at=? WHERE id=?",
                (page_count, warning, now_iso(), doc_id),
            )
            logger.info(
                "文档解析完成",
                extra={"extra_fields": {"doc_id": doc_id, "chunks": inserted, "chars": total_chars}},
            )
        except Exception as exc:  # noqa: BLE001 - 解析失败需落库状态
            logger.exception("文档解析失败", extra={"extra_fields": {"doc_id": doc_id}})
            db.execute(
                "UPDATE documents SET status='failed', error=?, updated_at=? WHERE id=?",
                (f"{type(exc).__name__}: {exc}", now_iso(), doc_id),
            )

    def _insert_chunks(
        self,
        db: Any,
        doc_id: str,
        chunks: Iterator[ChunkData],
        embedder: Any,
    ) -> int:
        """分批插入切片（batch=32），嵌入可用时写入向量 BLOB。"""
        inserted = 0
        batch: list[ChunkData] = []
        for chunk in chunks:
            batch.append(chunk)
            if len(batch) >= BATCH_SIZE:
                inserted += self._flush_batch(db, doc_id, batch, embedder, inserted)
                batch = []
        if batch:
            inserted += self._flush_batch(db, doc_id, batch, embedder, inserted)
        return inserted

    def _flush_batch(
        self,
        db: Any,
        doc_id: str,
        batch: list[ChunkData],
        embedder: Any,
        ordinal_start: int,
    ) -> int:
        """嵌入并落库一批切片。"""
        import json

        vectors = None
        dim = None
        model_name = None
        if embedder is not None:
            try:
                vectors, dim, model_name = embedder.embed([c.text for c in batch])
            except Exception as exc:  # noqa: BLE001 - 嵌入失败不阻断入库
                logger.warning(
                    "嵌入失败，降级为纯 FTS",
                    extra={"extra_fields": {"err": type(exc).__name__}},
                )
                vectors = None

        from ..utils.numpy_blob import encode_vector

        ts = now_iso()
        for offset, chunk in enumerate(batch):
            blob = None
            if vectors is not None and offset < len(vectors):
                blob = encode_vector(vectors[offset])
            db.execute(
                "INSERT INTO chunks(id, document_id, ordinal, text, text_seg, token_count, "
                "page_no, section, anchor, embedding, embedding_dim, embedding_model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(), doc_id, ordinal_start + offset, chunk.text, chunk.text_seg,
                    chunk.token_count, chunk.page_no, chunk.section,
                    json.dumps(chunk.anchor, ensure_ascii=False), blob, dim, model_name, ts,
                ),
            )
        return len(batch)

    # ── 查询 ────────────────────────────────────────────
    def get_document(self, doc_id: str) -> dict[str, Any]:
        """获取单个文档；不存在抛 ``1001``。"""
        db = get_db()
        row = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        if row is None:
            raise AppError(1001, "文档不存在", f"id={doc_id}")
        return row_to_document(row)

    def list_documents(
        self,
        collection: str | None = None,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页列出文档。

        Returns:
            ``(items, total)``。
        """
        db = get_db()
        clauses: list[str] = []
        params: list[Any] = []
        if collection:
            clauses.append("collection = ?")
            params.append(collection)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if q:
            clauses.append("title LIKE ?")
            params.append(f"%{q}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        total_row = db.query_one(f"SELECT COUNT(*) AS c FROM documents {where}", tuple(params))
        total = int(total_row["c"]) if total_row else 0
        offset = max(0, (page - 1) * page_size)
        rows = db.query_all(
            f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params) + (page_size, offset),
        )
        return [row_to_document(r) for r in rows], total

    def preview(self, doc_id: str, page_no: int | None = None) -> dict[str, Any]:
        """返回某页文本与该页切片锚点（供引用跳转定位）。

        Args:
            doc_id: 文档 id。
            page_no: 目标页码；``None`` 时取首页。

        Returns:
            ``{"page_no","pages","anchors"}``。
        """
        db = get_db()
        cfg = get_config()
        row = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        if row is None:
            raise AppError(1001, "文档不存在", f"id={doc_id}")

        target_page = page_no or 1
        pages: list[dict[str, Any]] = []
        rel_path = row["original_path"]
        path = cfg.files_dir / rel_path if rel_path else None
        if path is not None and path.exists():
            try:
                kwargs: dict[str, Any] = {}
                if row["fmt"] == "html":
                    kwargs["url"] = row["source_url"]
                blocks = parsers.parse(path, row["fmt"], **kwargs)
                grouped: dict[int, list[str]] = {}
                for block in blocks:
                    key = block.page_no or 1
                    grouped.setdefault(key, []).append(block.text)
                if target_page in grouped:
                    pages.append({"page_no": target_page, "text": "\n".join(grouped[target_page])})
                elif grouped:
                    first = sorted(grouped)[0]
                    target_page = first
                    pages.append({"page_no": first, "text": "\n".join(grouped[first])})
            except Exception:  # noqa: BLE001 - 预览失败不阻断
                logger.warning("预览解析失败", extra={"extra_fields": {"doc_id": doc_id}})

        import json

        anchor_rows = db.query_all(
            "SELECT id, section, anchor FROM chunks WHERE document_id = ? AND "
            "COALESCE(page_no, 1) = ? ORDER BY ordinal",
            (doc_id, target_page),
        )
        anchors: list[dict[str, Any]] = []
        for r in anchor_rows:
            anchor: dict[str, Any] = {}
            if r["anchor"]:
                try:
                    anchor = json.loads(r["anchor"])
                except (ValueError, TypeError):
                    anchor = {}
            anchors.append({"chunk_id": r["id"], "section": r["section"], "anchor": anchor})

        return {"page_no": target_page, "pages": pages, "anchors": anchors}

    def delete_document(self, doc_id: str) -> bool:
        """删除文档（级联删切片/FTS）与磁盘文件。"""
        db = get_db()
        cfg = get_config()
        row = db.query_one("SELECT original_path FROM documents WHERE id = ?", (doc_id,))
        if row is None:
            raise AppError(1001, "文档不存在", f"id={doc_id}")
        rel_path = row["original_path"]
        db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        if rel_path:
            target = cfg.files_dir / rel_path
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                logger.warning("删除原始文件失败", extra={"extra_fields": {"doc_id": doc_id}})
        logger.info("已删除文档", extra={"extra_fields": {"doc_id": doc_id}})
        return True

    def reparse(self, doc_id: str) -> dict[str, Any]:
        """重新解析（同步执行），返回最新文档。"""
        self.parse_document(doc_id)
        return self.get_document(doc_id)

    def document_file_path(self, doc_id: str) -> Path:
        """返回原始文件路径（供下载）。不存在抛 ``1001``。"""
        db = get_db()
        cfg = get_config()
        row = db.query_one("SELECT original_path, title FROM documents WHERE id = ?", (doc_id,))
        if row is None or not row["original_path"]:
            raise AppError(1001, "文档不存在或无可下载文件", f"id={doc_id}")
        path = cfg.files_dir / row["original_path"]
        if not path.exists():
            raise AppError(1001, "原始文件已丢失", f"id={doc_id}")
        return path

    def copy_existing(self, src: Path, filename: str, collection: str | None = None) -> str | None:
        """从本地路径复制一个文件入库（测试/迁移用）。"""
        data = Path(src).read_bytes()
        doc_id, _reason = self.create_from_bytes(filename, data, collection)
        return doc_id


def _get_embedder() -> Any:
    """惰性获取嵌入器（T05 提供；缺失时返回 ``None``，降级纯 FTS）。"""
    full_name = "backend.services.embedder"
    try:
        from .embedder import get_embedder
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == full_name or full_name.startswith(missing + "."):
            return None
        raise
    try:
        return get_embedder()
    except Exception as exc:  # noqa: BLE001 - 嵌入器初始化失败不阻断入库
        logger.warning("嵌入器不可用，降级纯 FTS：%s", type(exc).__name__)
        return None


_instance: IngestService | None = None


def get_ingest_service() -> IngestService:
    """返回进程级 :class:`IngestService` 单例。"""
    return IngestService.get_instance()
