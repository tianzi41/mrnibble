"""长期记忆路由（架构文档 §6.7）。

**红线 #5**：所有删除均为物理删除；召回唯一入口在
:class:`backend.services.memory.MemoryService.recall`，删除后结构上不可再召回。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import Response

from ..deps import get_database
from ..errors import AppError, ok
from ..models.memory import MemoryBulkDelete, MemoryCreate, MemoryPatch
from ..services.memory import MemoryService

router = APIRouter()

__all__ = ["router"]


@router.get("/memories")
def list_memories(
    type: str | None = Query(default=None, alias="type"),
    collection: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict:
    """分页列出记忆。"""
    items, total = MemoryService.get_instance().list(
        type_=type, collection=collection, page=page, page_size=page_size
    )
    return ok({"items": items, "total": total, "page": page, "page_size": page_size})


@router.post("/memories")
def create_memory(payload: MemoryCreate) -> dict:
    """新增记忆。"""
    mem = MemoryService.get_instance().add(
        payload.content,
        type_=payload.type,
        source="manual",
        confidence=payload.confidence,
        document_id=payload.document_id,
        collection=payload.collection,
    )
    return ok(mem)


@router.post("/memories/recall")
def recall_memories(payload: dict) -> dict:
    """按查询召回记忆（调试/前端联想用）。"""
    query = str(payload.get("query") or "").strip()
    # limit 也要防非数值：以前 int(...) 直接抛 → 裸 500（2026-09-19 修）
    try:
        limit = int(payload.get("limit") or 6)
    except (TypeError, ValueError):
        limit = 6
    items = MemoryService.get_instance().recall(query, limit=max(1, min(limit, 20)))
    return ok({"items": items})


@router.patch("/memories/{mid}")
def update_memory(mid: str, payload: MemoryPatch) -> dict:
    """编辑记忆。"""
    mem = MemoryService.get_instance().update(
        mid, content=payload.content, type_=payload.type
    )
    return ok(mem)


@router.delete("/memories/{mid}")
def delete_memory(mid: str) -> dict:
    """删除单条记忆（**硬删除**，同步清 FTS 索引与向量）。"""
    deleted = MemoryService.get_instance().delete(mid)
    if not deleted:
        raise AppError(1001, "记忆不存在")
    return ok({"deleted": True})


@router.post("/memories/bulk_delete")
def bulk_delete(payload: MemoryBulkDelete) -> dict:
    """批量删除记忆（硬删除）。"""
    n = MemoryService.get_instance().bulk_delete(payload.ids)
    return ok({"deleted_count": n})


@router.delete("/memories")
def clear_memories() -> dict:
    """清空全部记忆（硬删除）。"""
    n = MemoryService.get_instance().clear()
    return ok({"deleted_count": n})


@router.get("/memories/export")
def export_memories(format: str = Query(default="json", pattern="^(json|md|csv)$")) -> Response:
    """导出全部现存记忆为 JSON / Markdown / CSV。"""
    content, filename, mimetype = MemoryService.get_instance().export(format)
    return Response(
        content=content,
        media_type=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
