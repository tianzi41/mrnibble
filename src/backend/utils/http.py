"""统一的 ``httpx`` 客户端工厂（全项目唯一的出站 HTTP 入口）。

**为什么需要它**：``httpx`` 默认 ``trust_env=True``，会读取 ``HTTP_PROXY`` /
``HTTPS_PROXY`` 环境变量。当用户机器上装有 Clash / v2rayN / 公司代理等软件时，
这些变量往往指向本地端口，此时**发往本机模型端点（Ollama / 本服务 Mock）的请求
也会被丢给代理**，代理无法回连就返回 502「upstream connect failed」。
这是本地优先软件的经典坑，必须在客户端层规避。

策略：
- 目标是**回环地址**（127.0.0.1 / localhost / ::1）→ ``trust_env=False``，直连；
- 目标是**外部地址** → ``trust_env=True``，尊重系统代理（部分用户确实需要代理
  才能访问 OpenAI 等端点）；
- 环境变量 ``MRNIBBLE_DISABLE_PROXY=1`` 可强制全局禁用代理（排障用）。
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx

__all__ = ["is_loopback_url", "make_client"]

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}


def is_loopback_url(url: str) -> bool:
    """判断 URL 的主机是否为回环地址。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def make_client(base_url: str, *, timeout: httpx.Timeout | float, **kwargs) -> httpx.Client:
    """构造一个针对 ``base_url`` 的 ``httpx.Client``。

    Args:
        base_url: 目标端点（用于判断是否回环地址）。
        timeout: 超时策略。
        **kwargs: 其余参数透传给 :class:`httpx.Client`（如 ``follow_redirects``）。

    Returns:
        配置好代理策略的客户端。**调用方负责用 ``with`` 关闭。**
    """
    trust_env = True
    if os.environ.get("MRNIBBLE_DISABLE_PROXY", "").strip() in {"1", "true", "yes"}:
        trust_env = False
    elif is_loopback_url(base_url):
        trust_env = False
    return httpx.Client(timeout=timeout, trust_env=trust_env, **kwargs)
