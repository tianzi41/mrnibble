"""导出路由（架构文档 §6.9）。"""

from __future__ import annotations

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import FileResponse

from ..errors import AppError, ok
from ..models.export import ExportCreate
from ..services.export import ExportService

router = APIRouter()

__all__ = ["router"]


@router.post("/exports")
def create_export(payload: ExportCreate) -> dict:
    """导出产物为 Anki / Markdown / CSV。"""
    result = ExportService.get_instance().export(payload.generation_id, payload.format)
    return ok(result)


@router.post("/exports/image")
async def upload_export_image(
    generation_id: str = Query(description="导图产物 id"),
    image: UploadFile = File(description="PNG/SVG 图片"),
) -> dict:
    """保存前端 markmap 渲染的图片（PNG/SVG）。"""
    if not generation_id:
        raise AppError(1000, "缺少 generation_id")
    data = await image.read()
    if not data:
        raise AppError(1000, "图片内容为空")
    if len(data) > 20 * 1024 * 1024:
        raise AppError(3002, "图片过大")
    result = ExportService.get_instance().save_image(
        generation_id, image.filename or "mindmap.png", data
    )
    return ok(result)


@router.get("/exports/download")
def download_export(file: str = Query(description="相对 data/ 的文件路径")) -> FileResponse:
    """下载导出文件（含目录穿越校验）。"""
    path = ExportService.resolve_download(file)
    return FileResponse(
        str(path),
        filename=path.name,
        media_type="application/octet-stream",
    )
