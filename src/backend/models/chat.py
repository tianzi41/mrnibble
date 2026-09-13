"""会话与问答 schema（架构文档 §6.6）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "ChatMessageOut",
    "ConversationCreate",
    "ConversationPatch",
    "ConversationOut",
    "ChatRequest",
    "ChatResult",
    "CitationOut",
]

ConversationMode = Literal["normal", "guided"]


class CitationOut(BaseModel):
    """单条引用（页码/章节/锚点全部由服务端从 DB 回填）。"""

    n: int = Field(description="本轮引用序号，从 1 起")
    chunk_id: str
    document_id: str
    document_title: str
    page_no: int | None = None
    section: str | None = None
    anchor: dict[str, Any] = Field(default_factory=dict)
    snippet: str = ""


class ConversationCreate(BaseModel):
    """``POST /api/conversations`` 请求体。"""

    title: str | None = Field(default=None, max_length=200)
    mode: ConversationMode = "normal"
    document_ids: list[str] | None = None
    collection: str | None = None


class ConversationPatch(BaseModel):
    """``PATCH /api/conversations/{id}`` 请求体（重命名）。"""

    title: str = Field(min_length=1, max_length=200)


class ConversationOut(BaseModel):
    """会话对象。"""

    id: str
    title: str | None = None
    mode: str = "normal"
    document_ids: list[str] = Field(default_factory=list)
    collection: str | None = None
    message_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class ChatMessageOut(BaseModel):
    """消息对象。"""

    id: str
    conversation_id: str
    role: str
    content: str
    content_json: dict[str, Any] | None = None
    citations: list[CitationOut] = Field(default_factory=list)
    grounded: bool = True
    model: str | None = None
    created_at: str = ""


class ChatRequest(BaseModel):
    """``POST /api/chat`` 与 ``POST /api/chat/stream`` 请求体。"""

    conversation_id: str = Field(description="会话 id")
    message: str = Field(min_length=1, max_length=20000, description="用户输入")
    guided: bool = Field(default=False, description="是否引导式教学")
    document_ids: list[str] | None = Field(default=None, description="限定材料；None=全库")
    use_memory: bool = Field(default=True, description="是否注入长期记忆")
    grounding: Literal["strict", "loose"] = Field(
        default="strict",
        description="材料边界：strict=材料未命中时如实回「材料中未提及」；"
                    "loose=允许材料外回答并显式标注",
    )


class ChatResult(BaseModel):
    """``POST /api/chat``（非流式）响应。"""

    conversation_id: str
    message_id: str
    answer: str
    citations: list[CitationOut] = Field(default_factory=list)
    grounded: bool = True
    guided: dict[str, Any] | None = Field(default=None, description="引导式结构化载荷")
    retrieved: int = 0
    memory_used: int = 0
    model: str | None = None
