"""OpenAI 兼容 LLM 客户端（架构文档 §2 / §5）。

**刻意不使用 ``openai`` SDK**：本机环境装到 3.x 存在 API 变动风险，且国内各兼容端点
（DeepSeek / 智谱 / 百炼 / SiliconFlow / Ollama / vLLM）行为差异大，手写 ``httpx``
请求 + 自解析 SSE 最可控。

支持：
- 非流式 :meth:`LLMClient.chat` → 完整文本；
- 流式   :meth:`LLMClient.chat_stream` → 逐段增量（``yield str``）；
- 结构化 :meth:`LLMClient.chat_json` → 解析为 ``dict``（引导式教学与资料生成用）。

错误约定：
- 未配置端点 → :class:`AppError`(2000)；
- 网络失败 / 非法响应 → :class:`AppError`(2001)。
**所有日志与异常文本一律不含 api_key**（由 ``logging_setup`` 的脱敏过滤器兜底）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

import httpx

from ..utils.http import make_client

from ..deps import get_settings_service
from ..errors import AppError
from .events import log_event

logger = logging.getLogger(__name__)

__all__ = ["LLMClient", "get_llm_client"]

# 连接 / 读取超时（秒）。首 token 延迟目标 <3s，故 connect 收紧；读取放宽以容纳长回答。
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 180.0
_WRITE_TIMEOUT = 30.0
_POOL_TIMEOUT = 10.0

# 允许读取的最大响应字节数（防止异常端点无限输出）。
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _endpoint(base_url: str, path: str) -> str:
    """把 ``base_url`` 规范化为 ``{base}{path}``。

    兼容三种写法：
    - ``https://api.deepseek.com/v1``      → ``.../v1/chat/completions``
    - ``https://x/y/v1/``                  → 同上（末尾斜杠去除）
    - ``https://x/y/v1/chat/completions``  → 原样
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise AppError(2000, "模型未配置", "请先在设置页填写 base_url 与模型名")
    if base.endswith(path):
        return base
    return f"{base}{path}"


class LLMClient:
    """OpenAI 兼容对话客户端（进程级单例，配置每次调用时读取以支持热切换）。"""

    _instance: "LLMClient | None" = None

    @classmethod
    def get_instance(cls) -> "LLMClient":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 配置 ────────────────────────────────────────────
    def _config(self) -> dict[str, Any]:
        """读取 LLM 设置并解密 Key（**明文 Key 只存在于内存，绝不落日志**）。"""
        s = get_settings_service()
        return {
            "base_url": s.get("llm.base_url").strip(),
            "model": s.get("llm.model").strip(),
            "api_key": s.get_secret("llm.api_key"),
            "temperature": s.get_float("llm.temperature"),
            "max_tokens": s.get_int("llm.max_tokens"),
            "enable_thinking": s.get_bool("llm.enable_thinking"),
        }

    def is_configured(self) -> bool:
        """端点与模型名是否已配置（Key 可为空，Ollama 场景不需要）。"""
        cfg = self._config()
        return bool(cfg["base_url"] and cfg["model"])

    def ensure_configured(self) -> None:
        """未配置完整时抛出 2000，并在 detail 里说明**具体缺哪一项**。

        供问答/生成等上层编排在动手前调用。
        """
        self._ensure_configured(self._config())

    @staticmethod
    def _ensure_configured(cfg: dict[str, Any]) -> None:
        """未配置时抛出 2000，并明确告知**缺的是哪一项**。

        只填了接口地址却没填模型名是最常见的半配置状态，笼统的「模型未配置」
        会让人反复检查已经填好的地址。这里把缺失项直接写进 detail。
        """
        missing: list[str] = []
        if not cfg["base_url"]:
            missing.append("接口地址 base_url")
        if not cfg["model"]:
            missing.append("模型名")
        if missing:
            # message 直接带上缺失项：前端 Toast 只显示 message，写成笼统文案等于没提示。
            raise AppError(
                2000,
                "对话模型未配置完整：缺少" + "、".join(missing),
                "请在「设置 → 对话模型」中补全后重试",
            )

    def _headers(self, cfg: dict[str, Any]) -> dict[str, str]:
        """构造请求头（无 Key 时不携带 Authorization，兼容 Ollama）。"""
        headers = {"Content-Type": "application/json"}
        if cfg["api_key"]:
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        return headers

    def _body(
        self,
        cfg: dict[str, Any],
        messages: list[dict[str, str]],
        *,
        stream: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        """构造请求体。"""
        body: dict[str, Any] = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": cfg["temperature"] if temperature is None else temperature,
            "max_tokens": cfg["max_tokens"] if max_tokens is None else max_tokens,
            "stream": stream,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if cfg.get("enable_thinking"):
            # 深度思考：Qwen3 等混合推理模型的 OpenAI 兼容参数。
            # **只在开启时下发**——OpenAI 官方等严格端点会对未知字段直接报
            # 400，默认不发送可保证「关掉开关 = 与旧版行为完全一致」。
            body["enable_thinking"] = True
        return body

    def _timeout(self) -> httpx.Timeout:
        """统一超时策略。"""
        return httpx.Timeout(
            connect=_CONNECT_TIMEOUT,
            read=_READ_TIMEOUT,
            write=_WRITE_TIMEOUT,
            pool=_POOL_TIMEOUT,
        )

    # ── 非流式 ──────────────────────────────────────────
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        """非流式对话，返回完整文本。

        Raises:
            AppError: 2000 未配置；2001 调用失败。
        """
        cfg = self._config()
        self._ensure_configured(cfg)
        url = _endpoint(cfg["base_url"], "/chat/completions")
        payload = self._body(
            cfg, messages, stream=False,
            temperature=temperature, max_tokens=max_tokens, json_mode=json_mode,
        )
        started = time.perf_counter()
        try:
            with make_client(url, timeout=self._timeout()) as client:
                resp = client.post(url, json=payload, headers=self._headers(cfg))
        except httpx.HTTPError as exc:
            logger.warning("LLM 请求失败", extra={"extra_fields": {"type": type(exc).__name__}})
            raise AppError(2001, None, f"无法连接模型端点（{type(exc).__name__}）") from exc

        if resp.status_code >= 400:
            # 只透出状态码与截断后的响应头信息，避免把可能含敏感上下文的 body 打进日志。
            raise AppError(2001, None, f"模型端点返回 HTTP {resp.status_code}")

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            raise AppError(2001, None, "模型响应格式不符合 OpenAI 兼容约定") from exc

        log_event("llm_call", {
            "mode": "chat", "stream": False, "latency_ms": int((time.perf_counter() - started) * 1000),
            "chars": len(text),
        })
        return text

    # ── 流式 ────────────────────────────────────────────
    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """流式对话，逐段 ``yield`` 增量文本。

        Raises:
            AppError: 2000 未配置；2001 调用失败（在生成开始前抛出）。
        """
        cfg = self._config()
        self._ensure_configured(cfg)
        url = _endpoint(cfg["base_url"], "/chat/completions")
        payload = self._body(
            cfg, messages, stream=True, temperature=temperature, max_tokens=max_tokens
        )
        started = time.perf_counter()
        received = 0
        try:
            with make_client(url, timeout=self._timeout()) as client:
                with client.stream(
                    "POST", url, json=payload, headers=self._headers(cfg)
                ) as resp:
                    if resp.status_code >= 400:
                        raise AppError(
                            2001, None, f"模型端点返回 HTTP {resp.status_code}"
                        )
                    for line in resp.iter_lines():
                        if received > _MAX_RESPONSE_BYTES:
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        piece = delta.get("content") or ""
                        if piece:
                            received += len(piece.encode("utf-8"))
                            yield piece
        except AppError:
            raise
        except httpx.HTTPError as exc:
            logger.warning("LLM 流式请求失败", extra={"extra_fields": {"type": type(exc).__name__}})
            raise AppError(2001, None, f"流式连接中断（{type(exc).__name__}）") from exc

        log_event("llm_call", {
            "mode": "chat", "stream": True,
            "latency_ms": int((time.perf_counter() - started) * 1000), "chars": received,
        })

    # ── 结构化输出 ──────────────────────────────────────
    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """要求模型返回 JSON 对象并解析为 ``dict``。

        优先使用 ``response_format=json_object``；端点不支持时降级为普通请求后
        用 :func:`extract_json_object` 提取。解析失败 → :class:`AppError`(2001)。
        """
        try:
            text = self.chat(
                messages, temperature=temperature, max_tokens=max_tokens, json_mode=True
            )
            return extract_json_object(text)
        except AppError as exc:
            if exc.code != 2001:
                raise
        # 降级：不支持 response_format 的端点
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return extract_json_object(text)


# ── JSON 提取 ───────────────────────────────────────────
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_FIRST = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型文本中提取第一个 JSON 对象。

    依次尝试：直接解析 → ```json 代码块 → 首个 ``{...}`` 平衡块。
    全部失败抛 :class:`AppError`(2001)。
    """
    text = (text or "").strip()
    if not text:
        raise AppError(2001, None, "模型返回了空内容")
    candidates: list[str] = [text]
    candidates += [m.group(1) for m in _JSON_BLOCK.finditer(text)]
    m = _JSON_FIRST.search(text)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise AppError(2001, None, "模型未返回合法 JSON")


def get_llm_client() -> LLMClient:
    """返回 LLM 客户端单例。"""
    return LLMClient.get_instance()
