"""服务依赖装配（惰性单例）。

提供 :func:`get_database` 与 :func:`get_settings_service` 等访问入口，
避免各 router 直接构造服务、也避免循环导入（服务在函数体内惰性导入）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from .db.connection import Database
    from .services.settings_service import SettingsService

__all__ = ["get_database", "get_settings_service"]


def get_database() -> "Database":
    """返回进程级数据库单例（转调 :func:`backend.db.connection.get_db`）。"""
    from .db.connection import get_db

    return get_db()


def get_settings_service() -> "SettingsService":
    """返回进程级设置服务单例（首次调用时构建）。"""
    from .services.settings_service import SettingsService

    return SettingsService.get_instance()
