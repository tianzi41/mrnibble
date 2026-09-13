"""检索 schema（架构文档 §6.5）。

``Hit`` **只由服务端产生**：``page_no / section / anchor`` 全部来自 ``chunks`` 表，
绝不接受客户端或模型传入——这是引用页码不可伪造的结构保证。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = ["RetrievalRequest", "Hit", "RetrievalResult"]

RetrievalMode = Literal["hybrid", "fts", "vector"]


class RetrievalRequest(BaseModel):
    """``POST /api/retrieve`` 请求体。"""

    query: str = Field(description="查询文本")
    document_ids: list[str] | None = Field(default=None, description="限定文档；None=全库")
    collection: str | None = Field(default=None, description="限定分组")
    top_k: int = Field(default=8, ge=1, le=50)
    mode: RetrievalMode = Field(default="hybrid")


class Hit(BaseModel):
    """检索命中（页码等元数据均来自 DB）。"""

    chunk_id: str
    document_id: str
    document_title: str
    page_no: int | None = Field(default=None)
    section: str | None = Field(default=None)
    anchor: dict[str, Any] = Field(default_factory=dict)
    snippet: str = Field(default="")
    score: float = Field(default=0.0)
    fts_score: float | None = Field(default=None)
    vec_score: float | None = Field(default=None)


class RetrievalResult(BaseModel):
    """检索响应。"""

    hits: list[Hit] = Field(default_factory=list)
    latency_ms: int = 0
    mode: str = "hybrid"
    degraded: bool = Field(default=False, description="是否因嵌入不可用退化为纯 FTS")
