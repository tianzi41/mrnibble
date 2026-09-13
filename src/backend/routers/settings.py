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


@router.get("/settings/ollama/models", summary="列出本机 Ollama 模型")
def list_ollama_models() -> dict[str, Any]:
    """调用 Ollama ``/api/tags`` 返回模型名列表。"""
    service = get_settings_service()
    return ok({"models": service.list_ollama_models()})


@router.get("/settings/models", summary="从端点读取可用模型名")
def list_endpoint_models(
    target: Literal["llm", "embed"] = Query(default="llm", description="llm | embed"),
) -> dict[str, Any]:
    """调用端点 ``GET /models``，返回可用模型名（供设置页点选）。"""
    service = get_settings_service()
    return ok({"models": service.list_models(target)})
