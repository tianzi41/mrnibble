"""检索路由：``POST /api/retrieve``（架构文档 §6.5）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..errors import ok
from ..models.retrieval import RetrievalRequest
from ..services.retrieval import get_retrieval_service

__all__ = ["router"]

router = APIRouter()


@router.post("/retrieve", summary="混合检索（关键词 + 向量）")
def retrieve(payload: RetrievalRequest) -> dict[str, Any]:
    """执行混合检索并返回带页码的命中列表。"""
    service = get_retrieval_service()
    hits, latency_ms, degraded = service.hybrid_search(
        query=payload.query,
        document_ids=payload.document_ids,
        collection=payload.collection,
        top_k=payload.top_k,
        mode=payload.mode,
    )
    return ok(
        {
            "hits": hits,
            "latency_ms": latency_ms,
            "mode": payload.mode,
            "degraded": degraded,
        }
    )
