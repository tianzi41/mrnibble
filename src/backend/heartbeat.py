"""页面心跳（供桌面启动器判断「应用窗口是否还开着」）。

为什么需要它（2026-09-12 实测踩坑）：Edge 首次以 ``--app`` 打开时，可能把
URL **转交给一个已存在的实例后立即退出**。启动器原先以「浏览器子进程退出」
当作「用户关窗」，结果服务启动 1 秒就被关掉——页面自然显示
「127.0.0.1 拒绝连接」，用户关掉重开才正常。

修复思路：服务的生命周期改由**页面心跳**决定。前端每 5 秒 POST 一次
``/api/heartbeat``；启动器据此判断页面是否还活着，而不是依赖浏览器
进程的存活状态。线程安全（心跳与轮询在不同线程）。
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
# 初始值 = 进程启动时刻：从未收到心跳时，idle 从启动开始累计。
_last_seen: float = time.time()
_ever_seen: bool = False


def touch() -> None:
    """记录一次页面活动（心跳端点调用）。"""
    global _last_seen, _ever_seen
    with _lock:
        _last_seen = time.time()
        _ever_seen = True


def idle_seconds() -> float:
    """距上次心跳过了多少秒（从未收到过则从进程启动算起）。"""
    with _lock:
        return max(0.0, time.time() - _last_seen)


def ever_seen() -> bool:
    """启动以来是否收到过心跳。"""
    with _lock:
        return _ever_seen
