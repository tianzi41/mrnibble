"""路由汇总：统一以 ``/api`` 前缀注册所有子路由。

采用「按名导入 + 缺失跳过」策略，使得各阶段产物可以独立落地与自测：
模块尚未实现时（``ModuleNotFoundError`` 指向该模块本身）跳过并记录警告；
若模块存在但内部依赖缺失，则**照常抛出**，避免掩盖真实问题。
"""

from __future__ import annotations

import importlib
import logging

from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = ["register_routers"]

# (模块名, OpenAPI 标签) —— 顺序即文档展示顺序。
_ROUTER_SPECS: tuple[tuple[str, str], ...] = (
    ("system", "system"),
    ("settings", "settings"),
    ("documents", "documents"),
    ("retrieval", "retrieval"),
    ("chat", "chat"),
    ("memory", "memory"),
    ("generation", "generation"),
    ("courses", "courses"),
    ("export", "export"),
    ("voice", "voice"),
)


def _include(app: FastAPI, name: str, tag: str) -> None:
    """导入并注册单个子路由（模块本身缺失时跳过）。"""
    full_name = f"{__package__}.{name}"
    try:
        module = importlib.import_module(f".{name}", __package__)
    except ModuleNotFoundError as exc:
        if exc.name == full_name:
            logger.warning("路由模块尚未实现，已跳过：%s", full_name)
            return
        raise
    app.include_router(module.router, prefix="/api", tags=[tag])


def register_routers(app: FastAPI) -> None:
    """注册全部子路由到 ``app``（统一前缀 ``/api``）。"""
    for name, tag in _ROUTER_SPECS:
        _include(app, name, tag)
