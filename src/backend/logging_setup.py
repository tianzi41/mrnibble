"""日志初始化 + 密钥脱敏过滤器（安全红线 R-H05 / 架构文档 §11.3）。

安全设计：
- 所有日志输出为 **JSON Lines**，字段固定：``ts / level / logger / module / msg / extra``；
- :class:`SecretMaskingFilter` 提供两层脱敏：
    1. **注册表命中**：运行期把「当前所有密文对应的明文」注册进内存集合（不落盘），逐条替换为 ``***``；
    2. **正则兜底**：识别 ``sk-xxx`` / ``Bearer xxx`` / ``api_key=xxx`` / ``Authorization`` / ``token=`` 等形态；
- 对 ``msg`` 与 ``extra`` 均执行脱敏；
- 降级第三方噪音日志（httpx / httpcore / urllib3 / uvicorn.access 至 WARNING）。

验收（R-H05）：对 ``data/logs/*.log`` 全文检索 Key 明文必须 0 命中。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from .utils.timeutil import now_iso

__all__ = [
    "register_secret",
    "register_secrets",
    "clear_secrets",
    "mask_text",
    "SecretMaskingFilter",
    "JsonLinesFormatter",
    "setup_logging",
]

# ── 运行期密钥明文注册表（内存，不落盘）──────────────────
_SECRETS: set[str] = set()

# 正则兜底规则：命中即替换为 ***。
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"(?i)\bAuthorization\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\btoken\s*[:=]\s*\S+"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)([^\"',\s}]+)"),
)

_MASK = "***"


def register_secret(value: str | None) -> None:
    """把一条密钥明文登记进脱敏集合。

    Args:
        value: 密钥明文；``None`` 或空串忽略。长度 < 4 的值不做登记（避免误伤普通词）。
    """
    if not value or len(value) < 4:
        return
    _SECRETS.add(value)


def register_secrets(values: Iterable[str | None]) -> None:
    """批量登记密钥明文。"""
    for item in values:
        register_secret(item)


def clear_secrets() -> None:
    """清空脱敏集合（测试用）。"""
    _SECRETS.clear()


def mask_text(text: str) -> str:
    """对单段文本执行脱敏。

    Args:
        text: 原始文本。

    Returns:
        脱敏后的文本。
    """
    if not text:
        return text
    result = text
    # 1) 精确命中注册表（优先，长度降序避免子串问题）。
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret and secret in result:
            result = result.replace(secret, _MASK)
    # 2) 正则兜底。
    result = _SECRET_PATTERNS[0].sub(_MASK, result)
    result = _SECRET_PATTERNS[1].sub("Bearer " + _MASK, result)
    result = _SECRET_PATTERNS[2].sub("Authorization: " + _MASK, result)
    result = _SECRET_PATTERNS[3].sub("token=" + _MASK, result)
    result = _SECRET_PATTERNS[4].sub(lambda m: m.group(1) + _MASK, result)
    return result


def _mask_obj(value: Any) -> Any:
    """递归对 ``dict/list/str`` 执行脱敏。"""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: _mask_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_obj(v) for v in value]
    return value


class SecretMaskingFilter(logging.Filter):
    """日志脱敏过滤器：对 ``msg``（含 args 展开）与 ``extra_fields`` 脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            record.msg = mask_text(record.getMessage())
            record.args = ()
        except Exception:  # pragma: no cover - 脱敏失败不得影响业务日志
            pass
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, (dict, list, tuple, str)):
            try:
                record.extra_fields = _mask_obj(extra)
            except Exception:  # pragma: no cover
                record.extra_fields = {}
        return True


class JsonLinesFormatter(logging.Formatter):
    """JSON Lines 格式化器（字段固定，便于机器解析）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "msg": record.getMessage(),
            "extra": getattr(record, "extra_fields", {}) or {},
        }
        if record.exc_info:
            payload["extra"]["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured: bool = False


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    console: bool = True,
) -> Path:
    """初始化根日志（幂等：重复调用不会重复添加 handler）。

    Args:
        log_dir: 日志目录（``data/logs``）。
        level: 日志级别名。
        console: 是否同时输出到 stderr。

    Returns:
        写入的日志文件路径。
    """
    global _configured
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "mrnibble.log"

    root = logging.getLogger()
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric_level)

    if _configured:
        return log_file

    mask_filter = SecretMaskingFilter()
    formatter = JsonLinesFormatter()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(mask_filter)
    file_handler.setLevel(numeric_level)
    root.addHandler(file_handler)

    if console:
        # 控制台用简洁文本，但同样经过脱敏过滤器。
        stream_handler = logging.StreamHandler(stream=sys.stderr)
        stream_handler.setFormatter(
            logging.Formatter("%(levelname)s %(name)s: %(message)s")
        )
        stream_handler.addFilter(mask_filter)
        stream_handler.setLevel(numeric_level)
        root.addHandler(stream_handler)

    # 降级噪音日志。
    for noisy in ("httpx", "httpcore", "urllib3", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    logging.getLogger(__name__).info(
        "日志初始化完成", extra={"extra_fields": {"log_file": str(log_file)}}
    )
    return log_file
