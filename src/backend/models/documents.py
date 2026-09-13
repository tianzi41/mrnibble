"""文档/切片/解析状态 schema（架构文档 §6.4）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "Document",
    "DocumentList",
    "UploadResult",
    "SkippedFile",
    "UrlIngestRequest",
    "PreviewPage",
    "PreviewAnchor",
    "DocumentPreview",
]

# pending | parsing | ready | failed
DocStatus = Literal["pending", "parsing", "ready", "failed"]


class Document(BaseModel):
    """资料库条目（对前端公开）。"""

    id: str
    title: str
    source_type: str = Field(default="file", description="file | url")
    fmt: str = Field(description="pdf|docx|pptx|md|txt|html")
    page_count: int = Field(default=0, description="页数/幻灯片数")
    status: DocStatus = Field(default="pending")
    error: str | None = Field(default=None, description="失败原因")
    warning: str | None = Field(default=None, description="解析告警（如扫描版无文本层）")
    collection: str | None = Field(default=None)
    tags: list[str] = Field(default_factory=list)
    size_bytes: int | None = Field(default=None)
    created_at: str
    updated_at: str


class DocumentList(BaseModel):
    """文档列表响应。"""

    items: list[Document] = Field(default_factory=list)
    total: int = 0


class SkippedFile(BaseModel):
    """被跳过的文件（格式不支持/过大/重复等）。"""

    filename: str
    reason: str


class UploadResult(BaseModel):
    """批量上传结果。"""

    documents: list[Document] = Field(default_factory=list)
    skipped: list[SkippedFile] = Field(default_factory=list)


class UrlIngestRequest(BaseModel):
    """网页入库请求。"""

    url: str = Field(description="http/https 网页地址")
    collection: str | None = Field(default=None)


class PreviewPage(BaseModel):
    """预览页。"""

    page_no: int
    text: str


class PreviewAnchor(BaseModel):
    """页内切片锚点（点击引用定位用）。"""

    chunk_id: str
    section: str | None = None
    anchor: dict[str, Any] = Field(default_factory=dict)


class DocumentPreview(BaseModel):
    """文档预览响应。"""

    page_no: int
    pages: list[PreviewPage] = Field(default_factory=list)
    anchors: list[PreviewAnchor] = Field(default_factory=list)
