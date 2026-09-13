"""导出 schema（架构文档 §6.9）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["ExportCreate", "ExportResult", "ExportFormat"]

ExportFormat = Literal["anki", "markdown", "csv"]


class ExportCreate(BaseModel):
    """``POST /api/exports`` 请求体。"""

    generation_id: str = Field(description="要导出的生成产物 id")
    format: ExportFormat = Field(default="markdown")


class ExportResult(BaseModel):
    """导出结果。"""

    file_path: str = Field(description="相对 data/ 的路径")
    file_name: str
    size_bytes: int
    download_url: str
