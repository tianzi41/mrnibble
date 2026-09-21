"""资料库路由：上传 / 列表 / 详情 / 删除 / 重解析 / 预览 / 原始文件（架构文档 §6.4）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, UploadFile
from fastapi.responses import FileResponse

from ..config import get_config
from ..errors import ok
from ..models.documents import UrlIngestRequest
from ..services.ingest import get_ingest_service

__all__ = ["router"]

router = APIRouter()

# 常见 MIME（用于下载响应头）。
_MEDIA_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "md": "text/markdown",
    "txt": "text/plain",
    "html": "text/html",
}


@router.post("/documents/upload", summary="上传文档（可多选）")
async def upload_documents(
    background: BackgroundTasks,
    files: list[UploadFile] = File(..., description="文档文件，可多个"),
    collection: str | None = Form(default=None),
) -> dict[str, Any]:
    """保存上传文件并登记文档，随后在后台解析。"""
    service = get_ingest_service()
    documents: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    # 大小预检（2026-09-21 代码审查 P1-2）：`await upload.read()` 会把**整个文件**读进内存，
    # 而真正的大小校验在 `create_from_bytes` 里 —— 也就是说一个误拖的 1GB 文件会先占满内存
    # 再被拒绝。Starlette 解析 multipart 时已经填好 `upload.size`，所以先按它拦一道，
    # 超限的文件**根本不读**。（服务层的那道校验保留，作为兜底。）
    cfg = get_config()
    max_bytes = cfg.max_upload_mb * 1024 * 1024

    for upload in files:
        filename = upload.filename or "unnamed"
        pre_size = getattr(upload, "size", None)
        if isinstance(pre_size, int) and pre_size > max_bytes:
            skipped.append({"filename": filename,
                            "reason": f"文件过大（>{cfg.max_upload_mb}MB）"})
            continue
        try:
            data = await upload.read()
        except Exception as exc:  # noqa: BLE001
            skipped.append({"filename": filename, "reason": f"读取失败：{type(exc).__name__}"})
            continue
        doc_id, reason = service.create_from_bytes(filename, data, collection)
        if doc_id is None:
            skipped.append({"filename": filename, "reason": reason or "未创建"})
            continue
        background.add_task(service.parse_document, doc_id)
        documents.append(service.get_document(doc_id))

    return ok({"documents": documents, "skipped": skipped})


@router.post("/documents/url", summary="网页抓取入库")
def ingest_url(payload: UrlIngestRequest, background: BackgroundTasks) -> dict[str, Any]:
    """抓取网页正文并登记，随后在后台解析。"""
    service = get_ingest_service()
    doc_id = service.create_from_url(payload.url, payload.collection)
    background.add_task(service.parse_document, doc_id)
    return ok({"document": service.get_document(doc_id)})


@router.get("/documents", summary="文档列表")
def list_documents(
    collection: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """分页列出资料库文档。"""
    service = get_ingest_service()
    items, total = service.list_documents(collection, status, q, page, page_size)
    return ok({"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/documents/{doc_id}", summary="文档详情（前端轮询解析状态）")
def get_document(doc_id: str) -> dict[str, Any]:
    """返回单个文档详情。"""
    return ok({"document": get_ingest_service().get_document(doc_id)})


@router.delete("/documents/{doc_id}", summary="删除文档（级联删切片与 FTS）")
def delete_document(doc_id: str) -> dict[str, Any]:
    """删除文档及其全部切片/索引/原始文件。"""
    get_ingest_service().delete_document(doc_id)
    return ok({"deleted": True})


@router.post("/documents/{doc_id}/reparse", summary="重新解析")
def reparse_document(doc_id: str) -> dict[str, Any]:
    """同步重新解析文档。"""
    return ok({"document": get_ingest_service().reparse(doc_id)})


@router.get("/documents/{doc_id}/preview", summary="按页预览（引用跳转定位）")
def preview_document(
    doc_id: str,
    page_no: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """返回指定页文本与该页切片锚点。"""
    return ok(get_ingest_service().preview(doc_id, page_no))


@router.get("/documents/{doc_id}/file", summary="下载原始文件")
def download_document(doc_id: str) -> FileResponse:
    """流式返回原始文件（支持 Range）。"""
    service = get_ingest_service()
    doc = service.get_document(doc_id)
    path = service.document_file_path(doc_id)
    media = _MEDIA_TYPES.get(doc["fmt"], "application/octet-stream")
    return FileResponse(str(path), media_type=media, filename=doc["title"])
