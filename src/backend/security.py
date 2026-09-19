"""密钥安全：Fernet 加解密、Key 掩码、主密钥文件管理（架构文档 §11）。

安全约定：
- 主密钥 ``data/secret.key``：首次运行用 ``os.urandom(32)`` 生成，base64 存储，仅本机文件；
- 所有 ``is_secret=1`` 的设置值经 Fernet 加密后存 ``settings.value``；
- ``mask()`` 只保留前缀 3 + 后缀 4（如 ``sk-****abcd``），供 ``GET /api/settings`` 回显；
- **无任何接口返回密钥明文**；解密失败返回空串并告警，绝不抛出明文。
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .paths import data_path

logger = logging.getLogger(__name__)

__all__ = ["SecurityManager", "get_security_manager"]


def _is_fernet_key(raw: bytes) -> bool:
    """这段字节能不能被 Fernet 当成密钥用（32 字节 urlsafe-base64）。"""
    try:
        Fernet(raw)
        return True
    except (ValueError, TypeError):
        return False


class SecurityManager:
    """对称加密与掩码管理器（Fernet）。

    Attributes:
        key_path: 主密钥文件路径。
    """

    def __init__(self, key_path: Path | None = None) -> None:
        self.key_path: Path = Path(key_path) if key_path else data_path("secret.key")
        self._fernet: Fernet | None = None

    # ── 主密钥 ──────────────────────────────────────────
    def _load_or_create_key(self) -> bytes:
        """读取或首次生成主密钥。

        Returns:
            32 字节 urlsafe-base64 编码的 Fernet 密钥。

        Raises:
            OSError: 密钥文件无法创建/读取。
        """
        if self.key_path.exists():
            raw = self.key_path.read_bytes().strip()
            if raw and _is_fernet_key(raw):
                return raw
            # ⚠️ 文件「存在但内容为空/不是合法 Fernet 密钥」时**绝不能静默覆盖**：
            # 旧密钥一旦没了，用它加密的 settings（API Key 等）就永久解不开 ——
            # decrypt 只能返回空串，用户会以为"密钥丢了"，而且无从恢复。
            # 所以：先把坏文件改名备份（哪怕只是空文件，也可能是被截断的），再生成新的，
            # 并打一条明确的 error 日志告诉用户发生了什么、需要重新填哪些东西。
            reason = "内容为空" if not raw else "不是合法的 Fernet 密钥"
            broken = self.key_path.with_name(f"{self.key_path.name}.broken-{int(time.time())}")
            try:
                self.key_path.rename(broken)
                logger.error(
                    "主密钥文件%s，已备份为 %s 并重新生成新密钥；"
                    "此前用它加密的设置（如 API Key）需要重新填写",
                    reason, broken.name,
                )
            except OSError as exc:
                logger.error("主密钥文件%s，且备份失败（%s）；将直接重新生成", reason, exc)
        key = Fernet.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        # 以二进制写入（不含换行），仅本机可读。
        self.key_path.write_bytes(key)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        logger.info("已生成新的主密钥文件")
        return key

    @property
    def fernet(self) -> Fernet:
        """返回 Fernet 实例（惰性初始化）。"""
        if self._fernet is None:
            self._fernet = Fernet(self._load_or_create_key())
        return self._fernet

    @property
    def key_fingerprint(self) -> str:
        """返回主密钥指纹（sha256 前 12 位），可用于校验密钥一致性（不含明文）。"""
        import hashlib

        digest = hashlib.sha256(self._load_or_create_key()).hexdigest()
        return digest[:12]

    # ── 加解密 ──────────────────────────────────────────
    def encrypt(self, plain: str) -> str:
        """加密明文，返回 base64 令牌字符串。

        Args:
            plain: 明文（如 API Key）。

        Returns:
            加密后的令牌字符串；``plain`` 为空时返回空串（不加密空值）。
        """
        if plain is None or plain == "":
            return ""
        token = self.fernet.encrypt(plain.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, token: str) -> str:
        """解密令牌，返回明文；失败时返回空串。

        Args:
            token: ``encrypt`` 产出的令牌。

        Returns:
            明文；令牌为空或解密失败时返回空串。
        """
        if not token:
            return ""
        try:
            return self.fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            logger.warning("密钥解密失败（可能主密钥已更换），返回空值")
            return ""

    def is_encrypted(self, value: str) -> bool:
        """粗判字符串是否为合法 Fernet 令牌形态。"""
        if not value:
            return False
        try:
            base64.urlsafe_b64decode(value.encode("ascii"))
            return value.startswith("gAAAAA")
        except (ValueError, TypeError):
            return False

    # ── 掩码 ────────────────────────────────────────────
    @staticmethod
    def mask(secret: str | None, prefix: int = 3, suffix: int = 4) -> str:
        """生成密钥掩码（只留前缀 + 后缀）。

        Args:
            secret: 密钥明文。
            prefix: 保留前缀长度。
            suffix: 保留后缀长度。

        Returns:
            形如 ``sk-****abcd`` 的掩码；空值返回空串；过短则整体打码。
        """
        if not secret:
            return ""
        if len(secret) <= prefix + suffix:
            return "*" * len(secret)
        return f"{secret[:prefix]}****{secret[-suffix:]}"


_manager: SecurityManager | None = None


def get_security_manager() -> SecurityManager:
    """返回进程级 :class:`SecurityManager` 单例。"""
    global _manager
    if _manager is None:
        _manager = SecurityManager()
    return _manager
