"""ID 生成工具（架构文档 §14.7：主键统一 ``uuid4().hex``，32 位）。"""

from __future__ import annotations

import uuid

__all__ = ["new_id", "short_id"]


def new_id() -> str:
    """生成 32 位十六进制主键。

    Returns:
        形如 ``"a1b2c3..."`` 的 32 位小写十六进制字符串。
    """
    return uuid.uuid4().hex


def short_id(length: int = 8) -> str:
    """生成指定长度的短 ID（用于展示或非主键场景）。

    Args:
        length: 目标长度（1~32），越界自动截断到合法区间。

    Returns:
        截断后的十六进制字符串。
    """
    safe_len = max(1, min(int(length), 32))
    return uuid.uuid4().hex[:safe_len]
