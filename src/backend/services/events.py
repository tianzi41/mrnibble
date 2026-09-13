"""运行事件记录（架构文档 §4.2 ``events`` 表 / R-F04 可观测性）。

用于引导式教学状态迁移、检索耗时、LLM 调用等关键节点的落库观测。
**payload 必须已脱敏**：不得含密钥、用户文档原文全文（只允许片段截断与统计值）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..utils.timeutil import now_iso
from ..db.connection import get_db

logger = logging.getLogger(__name__)

__all__ = ["log_event"]


def log_event(kind: str, payload: dict[str, Any] | None = None) -> None:
    """写入一条运行事件。

    Args:
        kind: 事件类型，约定 ``guided_state`` / ``retrieval`` / ``llm_call``。
        payload: 已脱敏的结构化载荷；写库前序列化为 JSON。

    说明:
        事件表只用于观测，写失败不应影响主流程，因此内部吞掉异常仅记日志。
    """
    try:
        get_db().execute(
            "INSERT INTO events(ts, kind, payload) VALUES (?, ?, ?)",
            (now_iso(), kind, json.dumps(payload or {}, ensure_ascii=False)),
        )
    except Exception:  # pragma: no cover - 观测性写入失败不影响业务
        logger.warning("事件写入失败", exc_info=True)
