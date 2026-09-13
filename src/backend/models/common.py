"""通用 schema：统一响应封套与分页（架构文档 §6.1）。

除 SSE 与文件下载外的所有接口都返回统一封套::

    {"code": 0, "message": "ok", "data": {...}}
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = ["ErrorDetail", "Envelope", "PageMeta", "ok", "fail"]


class ErrorDetail(BaseModel):
    """失败封套中的技术细节（不含密钥/敏感路径）。"""

    detail: str = Field(default="", description="技术细节，用于排障")


class Envelope(BaseModel):
    """统一响应封套。"""

    code: int = Field(default=0, description="0=成功，非 0 见错误码表")
    message: str = Field(default="ok", description="可直接展示的中文摘要")
    data: Any = Field(default=None, description="业务数据；失败时为 null")
    error: ErrorDetail | None = Field(default=None, description="可选技术细节")


class PageMeta(BaseModel):
    """分页元信息。"""

    page: int = Field(default=1, ge=1, description="页码，从 1 起")
    page_size: int = Field(default=20, ge=1, le=200, description="每页条数")
    total: int = Field(default=0, ge=0, description="总条数")


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    """构造成功封套字典。"""
    return {"code": 0, "message": message, "data": data}


def fail(code: int, message: str, detail: str | None = None) -> dict[str, Any]:
    """构造失败封套字典。"""
    payload: dict[str, Any] = {"code": int(code), "message": message, "data": None}
    if detail:
        payload["error"] = {"detail": detail}
    return payload
