"""时间工具（架构文档 §14.6：全项目时间戳一律 UTC ISO8601，秒级带 ``Z``）。"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["now_iso", "to_iso", "parse_iso", "now_epoch_ms"]


def now_iso() -> str:
    """返回当前 UTC 时间戳（秒级，``Z`` 结尾）。

    Returns:
        形如 ``"2026-09-11T08:30:00Z"`` 的字符串。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso(dt: datetime) -> str:
    """把 ``datetime`` 归一到 UTC ISO8601（秒级，``Z`` 结尾）。

    Args:
        dt: 任意 ``datetime``；无时区信息时按 UTC 处理。

    Returns:
        UTC ISO8601 字符串。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """解析 ISO8601 字符串为带时区的 ``datetime``。

    Args:
        value: ISO8601 字符串（允许 ``Z`` 结尾）。

    Returns:
        带 UTC 时区的 ``datetime``。

    Raises:
        ValueError: 字符串无法解析时抛出。
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_epoch_ms() -> int:
    """返回当前 Unix 毫秒时间戳（用于耗时统计）。"""
    return int(datetime.now(timezone.utc).timestamp() * 1000)
