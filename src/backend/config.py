"""应用配置：读取 ``.env`` 与环境变量，提供进程级单例。

约定（见架构文档 §14.2）：
- 业务代码**禁止**散落 ``os.environ`` 读取，一律经 :func:`get_config` 拿到 :class:`AppConfig`。
- 运行期可变配置（模型 / Key / 开关）不走本模块，而是经 ``services.settings_service`` 读写。

主理人拍板决策：
- 端口固定默认 ``8760``，被占用则向上探测至 ``8770``（最多），并写 ``data/runtime.json``。
- 数据目录默认程序目录内 ``data/``（绿色便携），可用 ``ZHIBAN_DATA_DIR`` 覆盖。
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import __version__
from .paths import data_path, data_root, is_frozen, resource_path

__all__ = ["AppConfig", "get_config", "reload_config", "select_port", "write_runtime_json"]


def _load_dotenv_once() -> None:
    """加载项目根目录的 ``.env``（若存在）。重复调用无副作用。"""
    env_file = resource_path(".env")
    if env_file.exists():
        load_dotenv(env_file, override=False)


def _env_str(name: str, default: str) -> str:
    """读取字符串型环境变量，空串视为未设置。"""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量，非法值回退默认值。"""
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class AppConfig:
    """进程级应用配置（不可变语义，需要变更时重建）。

    Attributes:
        host: 监听地址，固定回环地址，绝不绑 ``0.0.0.0``。
        port_pref: 端口首选值；为 ``0`` 时表示完全随机。
        port_max: 端口顺延上界（含）。
        data_dir: 运行期数据根目录。
        log_level: 日志级别。
        max_upload_mb: 单文件上传大小上限（MB）。
        frozen: 是否冻结态。
        version: 应用版本号。
    """

    host: str = "127.0.0.1"
    port_pref: int = 8760
    port_max: int = 8770
    data_dir: Path = field(default_factory=data_root)
    log_level: str = "INFO"
    max_upload_mb: int = 200
    frozen: bool = field(default_factory=is_frozen)
    version: str = __version__

    # ── 派生路径 ────────────────────────────────────────
    @property
    def db_path(self) -> Path:
        """SQLite 主库文件路径。"""
        return data_path("zhiban.db")

    @property
    def files_dir(self) -> Path:
        """上传原始文档目录。"""
        return data_path("files")

    @property
    def exports_dir(self) -> Path:
        """导出产物目录。"""
        return data_path("exports")

    @property
    def logs_dir(self) -> Path:
        """日志目录。"""
        return data_path("logs")

    @property
    def secret_key_path(self) -> Path:
        """Fernet 主密钥文件路径。"""
        return data_path("secret.key")

    @property
    def runtime_path(self) -> Path:
        """运行态文件（port/pid/url）路径。"""
        return data_path("runtime.json")

    @property
    def is_random_port(self) -> bool:
        """是否要求完全随机端口。"""
        return self.port_pref == 0


_config: AppConfig | None = None

# 允许的监听地址（单机软件红线：只绑回环，绝不绑 0.0.0.0）。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def get_config() -> AppConfig:
    """返回进程级配置单例（首次调用时构建）。"""
    global _config
    if _config is None:
        _load_dotenv_once()
        host = _env_str("ZHIBAN_HOST", "127.0.0.1")
        # 2026-09-21 代码审查 P2-2：原实现允许 `.env` 把 host 覆盖成 0.0.0.0，
        # 与类文档里「绝不绑 0.0.0.0」的声明不符 —— 绑到外网卡会把资料库与
        # 模型 Key 暴露给同网段的其他人。这里直接拒绝，把红线做成可执行约束。
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"ZHIBAN_HOST 只允许回环地址（当前 {host!r}）：知伴是单机软件，"
                f"绑定其它地址会让同网段的人访问到你的资料库与模型 Key。"
            )
        _config = AppConfig(
            host=host,
            port_pref=_env_int("ZHIBAN_PORT", 8760),
            port_max=_env_int("ZHIBAN_PORT_MAX", 8770),
            data_dir=data_root(),
            log_level=_env_str("ZHIBAN_LOG_LEVEL", "INFO").upper(),
            max_upload_mb=_env_int("ZHIBAN_MAX_UPLOAD_MB", 200),
            frozen=is_frozen(),
            version=__version__,
        )
    return _config


def reload_config() -> AppConfig:
    """清空缓存并重建配置（测试用；生产运行期不调用）。"""
    global _config
    _config = None
    return get_config()


def _port_available(host: str, port: int) -> bool:
    """探测给定端口是否可绑定。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def select_port(cfg: AppConfig | None = None) -> int:
    """选择一个可用端口。

    规则：``port_pref==0`` 时由系统分配随机端口；否则从 ``port_pref`` 起，
    向上探测到 ``port_max``（含），返回第一个可绑定端口。

    Args:
        cfg: 配置对象，默认取单例。

    Returns:
        可绑定的端口号。

    Raises:
        RuntimeError: 在 ``[port_pref, port_max]`` 内无可用端口。
    """
    cfg = cfg or get_config()
    if cfg.is_random_port:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((cfg.host, 0))
            return int(sock.getsockname()[1])
    for port in range(cfg.port_pref, cfg.port_max + 1):
        if _port_available(cfg.host, port):
            return port
    raise RuntimeError(
        f"端口 {cfg.port_pref}-{cfg.port_max} 均被占用，无法启动知伴服务"
    )


def write_runtime_json(port: int, cfg: AppConfig | None = None) -> Path:
    """把运行态写入 ``data/runtime.json``，便于调试与外部工具读取。

    Args:
        port: 实际监听端口。
        cfg: 配置对象，默认取单例。

    Returns:
        写入的文件路径。
    """
    from .utils.timeutil import now_iso

    cfg = cfg or get_config()
    payload: dict[str, Any] = {
        "port": port,
        "pid": os.getpid(),
        "started_at": now_iso(),
        "url": f"http://{cfg.host}:{port}",
        "version": cfg.version,
    }
    path = cfg.runtime_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
