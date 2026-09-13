"""应用装配与进程启动入口（架构文档 §1.2 / §6）。

职责：
- :func:`create_app` —— 装配路由、异常处理、静态资源挂载、启动/停机钩子；
- :func:`run_server` —— 选端口 → 写 ``runtime.json`` → 运行 uvicorn（供启动器/开发脚本调用）。
"""

from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import get_config, select_port, write_runtime_json
from .deps import get_database
from .errors import register_exception_handlers
from .logging_setup import setup_logging
from .paths import ensure_data_dirs, resource_path
from .routers import register_routers

logger = logging.getLogger(__name__)


def _register_secret_masking() -> None:
    """把当前已存密钥的明文登记进日志脱敏集合（T03 提供实现，缺失时跳过）。"""
    full_name = "backend.services.settings_service"
    try:
        from .services.settings_service import SettingsService
    except ModuleNotFoundError as exc:
        # 目标模块尚未实现（含父包 backend.services 都不存在）时跳过；其它缺失照常抛出。
        missing = exc.name or ""
        if missing == full_name or full_name.startswith(missing + "."):
            return
        raise
    SettingsService.get_instance().register_secrets()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动建目录/日志/建库迁移；停机 checkpoint。"""
    cfg = get_config()
    ensure_data_dirs()
    setup_logging(cfg.logs_dir, cfg.log_level)

    # 先建库/迁移（settings 表须已存在），再登记密钥脱敏。
    db = get_database()
    db.migrate()
    _register_secret_masking()

    logger.info(
        "知伴服务启动完成",
        extra={"extra_fields": {"version": cfg.version, "data_dir": str(cfg.data_dir)}},
    )
    try:
        yield
    finally:
        try:
            db.checkpoint()
            db.close_all()
        finally:
            logger.info("知伴服务已停止")


def _register_static_mime_types() -> None:
    """补齐静态资源所需的 MIME 映射。

    ``.mjs``（ES module，pdf.js 的构建产物）在 Python 的 ``mimetypes`` 里被识别为
    ``text/plain``；而浏览器对 module 脚本**强制校验 MIME**，``text/plain`` 会被
    直接拒绝执行（表现为「组件静默不加载」）。这里显式声明为 JS。
    """
    mimetypes.add_type("text/javascript", ".mjs")
    mimetypes.add_type("application/wasm", ".wasm")


def create_app() -> FastAPI:
    """创建并装配 FastAPI 应用。"""
    _register_static_mime_types()
    app = FastAPI(
        title="知伴 ZhiBan",
        description="单机单人的本地 AI 学习助手（本机侧车服务）",
        version=__version__,
        lifespan=_lifespan,
    )

    register_exception_handlers(app)
    register_routers(app)

    web_dir = resource_path("web")
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        """返回前端单页外壳。"""
        return FileResponse(str(resource_path("web", "index.html")))

    return app


# 供 `uvicorn backend.main:app` 使用。
app = create_app()


def run_server(reload: bool = False) -> None:
    """选择端口、写运行态并启动 uvicorn（阻塞直至退出）。

    Args:
        reload: 开发态是否开启自动重载（开启时必须以导入字符串方式启动）。
    """
    cfg = get_config()
    setup_logging(cfg.logs_dir, cfg.log_level)

    port = select_port(cfg)
    write_runtime_json(port, cfg)
    logger.info(
        "启动知伴服务",
        extra={"extra_fields": {"host": cfg.host, "port": port, "reload": reload}},
    )
    uvicorn.run(
        "backend.main:app",
        host=cfg.host,
        port=port,
        reload=reload,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover - 手动启动入口
    run_server(reload=False)
