"""会话与问答路由（架构文档 §6.6）。

``POST /api/chat/stream`` 返回 SSE（``text/event-stream``），
事件类型见 §6.1：``meta / delta / guided / citation / done / error``。
"""

from __future__ import annotations

import json
from typing import Iterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..deps import get_database
from ..errors import AppError, ok
from ..models.chat import ChatRequest, ConversationCreate, ConversationPatch
from ..services.chat import ChatService

router = APIRouter()

__all__ = ["router"]

_SSE_HEARTBEAT_SECONDS = 15


def _sse(event: str, data: dict) -> str:
    """把一个事件编码为 SSE 帧。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/conversations")
def create_conversation(payload: ConversationCreate) -> dict:
    """创建会话。"""
    svc = ChatService.get_instance()
    return ok(svc.create_conversation(payload.model_dump()))


@router.get("/conversations")
def list_conversations() -> dict:
    """会话列表。"""
    return ok(ChatService.get_instance().list_conversations())


@router.get("/conversations/{cid}")
def get_conversation(cid: str) -> dict:
    """会话详情（含消息）。"""
    conv = ChatService.get_instance().get_conversation(cid)
    if conv is None:
        raise AppError(1001, "会话不存在")
    return ok(conv)


@router.patch("/conversations/{cid}")
def rename_conversation(cid: str, payload: ConversationPatch) -> dict:
    """重命名会话。"""
    ChatService.get_instance().rename_conversation(cid, payload.title)
    return ok({"updated": True})


@router.delete("/conversations/{cid}")
def delete_conversation(cid: str) -> dict:
    """删除会话（级联消息）。"""
    ChatService.get_instance().delete_conversation(cid)
    return ok({"deleted": True})


@router.post("/chat/stream")
def chat_stream(payload: ChatRequest) -> StreamingResponse:
    """流式问答（SSE）。"""
    def gen() -> Iterator[str]:
        for ev in ChatService.get_instance().stream_events(payload):
            yield _sse(ev["event"], ev["data"])

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat")
def chat(payload: ChatRequest) -> dict:
    """非流式问答（联调与自动化测试用，与流式共用同一流水线）。"""
    result = ChatService.get_instance().run_sync(payload)
    return ok(result)
