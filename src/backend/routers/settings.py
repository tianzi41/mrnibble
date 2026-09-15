"""设置路由：读写设置、连接测试、Ollama 模型列表（架构文档 §6.10）。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query

from ..deps import get_settings_service
from ..errors import ok
from ..models.settings import SettingsTestRequest, SettingsUpdate

__all__ = ["router"]

router = APIRouter()


@router.get("/settings", summary="读取公开设置（密钥仅返回掩码）")
def get_settings() -> dict[str, Any]:
    """返回公开设置视图，**永不包含密钥明文**。"""
    service = get_settings_service()
    return ok(service.get_public())


@router.put("/settings", summary="更新设置（密钥加密入库）")
def update_settings(payload: SettingsUpdate) -> dict[str, Any]:
    """应用分组更新；密钥项为空串时保持原值不变。"""
    service = get_settings_service()
    # exclude_none 避免把未提供的字段覆盖为 None。
    patch = payload.model_dump(exclude_none=True)
    updated = service.update(patch)
    return ok({"updated": updated, "settings": service.get_public()})


@router.post("/settings/test", summary="连接测试")
def test_settings(payload: SettingsTestRequest) -> dict[str, Any]:
    """测试 llm / embed / tts / ollama 连通性。"""
    service = get_settings_service()
    return ok(service.test(payload.target))


@router.get("/settings/embed/status", summary="本地 bge 语义模型就绪状态")
def embed_status() -> dict[str, Any]:
    """设置页展示「bge 模型是否已下载」与当前本地引擎。"""
    from ..services.embedder import get_embedder

    service = get_settings_service()
    cfg = service.get_public().get("embed", {})
    emb = get_embedder()
    bge = emb._get_bge({   # noqa: SLF001 - 路由层读取就绪状态，复用门面解析逻辑
        "local_dir": (cfg or {}).get("local_dir") or "models/embed/bge-small-zh-v1.5",
    })
    return ok({
        "local_engine": (cfg or {}).get("local_engine") or "hash",
        "bge_available": bool(bge),
        "bge_dir": str(bge.dir) if bge else "",
    })


@router.get("/settings/ollama/models", summary="列出本机 Ollama 模型")
def list_ollama_models() -> dict[str, Any]:
    """调用 Ollama ``/api/tags`` 返回模型名列表。"""
    service = get_settings_service()
    return ok({"models": service.list_ollama_models()})


@router.get("/settings/models", summary="从端点读取可用模型名")
def list_endpoint_models(
    target: Literal["llm", "embed", "tts"] = Query(default="llm", description="llm | embed | tts"),
) -> dict[str, Any]:
    """调用端点 ``GET /models``，返回可用模型名（供设置页点选）。"""
    service = get_settings_service()
    return ok({"models": service.list_models(target)})
