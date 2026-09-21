"""系统路由：健康检查、系统信息、统计、优雅关闭（架构文档 §6.3）。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from fastapi import APIRouter

from .. import __version__
from .. import heartbeat
from ..config import get_config
from ..deps import get_database
from ..errors import ok
from ..paths import data_path, resource_path

__all__ = ["router"]

router = APIRouter()

# 进程启动时间（用于 uptime 计算）。
_START_TS: float = time.time()


def _read_port() -> int:
    """读取实际监听端口：优先 ``runtime.json``，回退配置首选端口。"""
    cfg = get_config()
    runtime_file = cfg.runtime_path
    try:
        if runtime_file.exists():
            data = json.loads(runtime_file.read_text(encoding="utf-8"))
            port = data.get("port")
            if isinstance(port, int):
                return port
    except (OSError, ValueError):
        pass
    return cfg.port_pref


@router.get("/health", summary="健康检查")
def health() -> dict[str, Any]:
    """返回服务健康状态。

    ``heartbeat`` 子对象是**诊断用**（2026-09-20 加）：桌面启动器据此判断
    「页面是否还活着」，而这段信息以前完全不可观测——用户遇到「挂后台后
    后台自己退出」时只能靠猜。现在可以直接读 ``idle_s`` 看心跳是否还在。
    """
    return ok(
        {
            "status": "ok",
            "version": __version__,
            "port": _read_port(),
            "uptime_s": round(time.time() - _START_TS, 3),
            "heartbeat": {
                "idle_s": round(heartbeat.idle_seconds(), 2),
                "ever_seen": heartbeat.ever_seen(),
                "bye": heartbeat.bye_seen(),
            },
        }
    )


@router.post("/heartbeat", summary="页面心跳（桌面启动器据此判断窗口是否存活）")
def page_heartbeat(bye: int = 0) -> dict[str, Any]:
    """前端每 5 秒调用一次；启动器以「窗口是否还在 + 心跳是否持续」决定何时停服务。

    背景：Edge 首开可能把 URL 转交给已有实例后立即退出，浏览器子进程的
    存活状态**不可靠**（曾导致服务启动 1 秒就被关掉、页面显示拒绝连接）。
    而只靠心跳也不够稳：窗口最小化时浏览器会节流隐藏页的定时器，心跳被拉长
    （2026-09-20 实测停服事故）。所以启动器还会独立探测应用窗口是否存在。

    ``?bye=1``：页面在 ``pagehide`` 时用 ``navigator.sendBeacon`` 发一次，
    表示「用户真的在关窗」，让启动器走快路径立刻停机（不必等心跳宽限）。
    """
    heartbeat.touch()
    if bye:
        heartbeat.mark_bye()
    return ok({"ok": True})


@router.get("/system/info", summary="系统信息")
def system_info() -> dict[str, Any]:
    """返回版本、数据目录、模型目录、离线提示等。"""
    cfg = get_config()
    asr_dir = resource_path("models", "asr", "sense-voice-small")
    embed_dir = resource_path("models", "embed")
    return ok(
        {
            "version": cfg.version,
            "port": _read_port(),
            "data_dir": str(cfg.data_dir),
            "frozen": cfg.frozen,
            "offline_hint": "断网时请切换到本地模型端点（Ollama），可离线问答与检索。",
            "models": {
                "asr": str(asr_dir) if asr_dir.exists() else None,
                "embed": str(embed_dir) if embed_dir.exists() else None,
            },
        }
    )


@router.get("/system/stats", summary="资料/切片/会话/记忆计数")
def system_stats() -> dict[str, Any]:
    """返回各主要表的行数统计。"""
    db = get_database()

    def _count(table: str) -> int:
        try:
            row = db.query_one(f"SELECT COUNT(*) AS c FROM {table}")
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    return ok(
        {
            "documents": _count("documents"),
            "chunks": _count("chunks"),
            "conversations": _count("conversations"),
            "messages": _count("messages"),
            "memories": _count("memories"),
            "generations": _count("generations"),
            "flashcards": _count("flashcards"),
        }
    )


def _delayed_exit(delay_s: float = 0.3) -> None:
    """延迟短暂时间后强制结束进程（给响应留出回写时间）。

    ``os._exit`` 会跳过 atexit 与缓冲区刷写，停机前最后几条日志可能丢
    （2026-09-21 代码审查 P2-6）。所以先显式 ``logging.shutdown()`` 把文件句柄
    那一侧刷干净，再退出。数据库安全不依赖这里：WAL 本身是崩溃安全的，
    正常关窗路径还会走 lifespan 的 checkpoint。
    """
    time.sleep(delay_s)
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001 - 退出路径上不允许再抛
        pass
    os._exit(0)


@router.post("/system/shutdown", summary="优雅关闭知伴")
def system_shutdown() -> dict[str, Any]:
    """响应后退出进程（供界面「退出知伴」按钮调用）。"""
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return ok({"shutting_down": True})
