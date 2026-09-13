"""长期记忆 schema（架构文档 §6.7）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = ["MemoryCreate", "MemoryPatch", "MemoryBulkDelete", "MemoryOut", "MemoryType"]

MemoryType = Literal["preference", "progress", "knowledge_gap", "fact"]


class MemoryCreate(BaseModel):
    """``POST /api/memories`` 请求体。"""

    type: MemoryType = Field(default="fact", description="记忆类型")
    content: str = Field(min_length=1, max_length=2000, description="记忆内容")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    document_id: str | None = None
    collection: str | None = None


class MemoryPatch(BaseModel):
    """``PATCH /api/memories/{id}`` 请求体。"""

    type: MemoryType | None = None
    content: str | None = Field(default=None, min_length=1, max_length=2000)


class MemoryBulkDelete(BaseModel):
    """``POST /api/memories/bulk_delete`` 请求体。"""

    ids: list[str] = Field(min_length=1, description="要删除的记忆 id 列表")


class MemoryOut(BaseModel):
    """记忆对象（不含向量等内部字段）。"""

    id: str
    type: str
    content: str
    source: str = "manual"
    confidence: float = 0.5
    document_id: str | None = None
    conversation_id: str | None = None
    recall_count: int = 0
    last_referenced_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    score: float | None = Field(default=None, description="仅召回接口返回")
