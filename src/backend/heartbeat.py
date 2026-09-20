"""页面心跳（供桌面启动器判断「应用窗口是否还开着」）。

为什么需要它（2026-09-12 实测踩坑）：Edge 首次以 ``--app`` 打开时，可能把
URL **转交给一个已存在的实例后立即退出**。启动器原先以「浏览器子进程退出」
当作「用户关窗」，结果服务启动 1 秒就被关掉——页面自然显示
「127.0.0.1 拒绝连接」，用户关掉重开才正常。

修复思路：服务的生命周期改由**页面心跳**决定。前端每 5 秒 POST 一次
``/api/heartbeat``；启动器据此判断页面是否还活着，而不是依赖浏览器
进程的存活状态。线程安全（心跳与轮询在不同线程）。

⚠️ 2026-09-20 补充：心跳**不再是唯一判据**。用户把窗口最小化后放着不动，
浏览器会节流/冻结隐藏页的 JS 定时器，5 秒心跳被拉长到十几秒，启动器误判
「窗口已关」把服务停掉（日志实证）。现在启动器先看**窗口是否还在**
（枚举顶层窗口标题，不依赖页面 JS），只在窗口确实消失后才用心跳；
另外页面在 ``pagehide`` 时用 ``sendBeacon`` 发一次 ``?bye=1``，
让「真的关窗」这条路径能立刻停机。
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
# 初始值 = 进程启动时刻：从未收到心跳时，idle 从启动开始累计。
_last_seen: float = time.time()
_ever_seen: bool = False
# 页面主动告别（pagehide → sendBeacon）。用于「用户真的关了窗口」时**立即**停机，
# 不必等心跳宽限——这也是最小化场景之外的快路径。
_bye: bool = False


def touch() -> None:
    """记录一次页面活动（心跳端点调用）。"""
    global _last_seen, _ever_seen
    with _lock:
        _last_seen = time.time()
        _ever_seen = True


def mark_bye() -> None:
    """记录「页面正在卸载」（用户关了窗口 / 导航离开）。"""
    global _bye
    with _lock:
        _bye = True


def bye_seen() -> bool:
    """是否收到过页面告别信号。"""
    with _lock:
        return _bye


def idle_seconds() -> float:
    """距上次心跳过了多少秒（从未收到过则从进程启动算起）。"""
    with _lock:
        return max(0.0, time.time() - _last_seen)


def ever_seen() -> bool:
    """启动以来是否收到过心跳。"""
    with _lock:
        return _ever_seen
