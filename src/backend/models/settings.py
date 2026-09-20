"""设置相关 schema（架构文档 §6.10）。

- :class:`SettingsUpdate` —— ``PUT /api/settings`` 请求体（分组、字段可省略）。
- :class:`SettingsTestRequest` —— ``POST /api/settings/test`` 请求体。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LLMSettings",
    "EmbedSettings",
    "TTSSettings",
    "ASRSettings",
    "UISettings",
    "RetrievalSettings",
    "MemorySettings",
    "SettingsUpdate",
    "SettingsTestRequest",
]


class LLMSettings(BaseModel):
    """对话模型设置。``api_key`` 为密文项，空串表示保持原值。"""

    base_url: str | None = Field(default=None, description="OpenAI 兼容端点（含 /v1）")
    model: str | None = Field(default=None, description="模型名，如 deepseek-chat")
    api_key: str | None = Field(default=None, description="API Key（加密入库，回显掩码）")
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    enable_thinking: bool | None = Field(
        default=None,
        description="深度思考：仅开启时随请求下发 enable_thinking=true（Qwen3 等混合推理模型）",
    )


class EmbedSettings(BaseModel):
    """嵌入模型设置。"""

    provider: Literal["cloud", "local", "auto"] | None = Field(default=None)
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    # 本地引擎：hash（默认，零下载）| bge（语义模型，需先运行 download_bge_model.py）
    local_engine: Literal["hash", "bge"] | None = Field(default=None)
    local_dir: str | None = Field(default=None, description="本地 bge 模型目录")


class TTSSettings(BaseModel):
    """语音朗读设置（默认关闭）。"""

    enabled: bool | None = None
    mode: Literal["off", "local", "cloud"] | None = None
    base_url: str | None = None
    model: str | None = None
    voice: str | None = None
    api_key: str | None = None
    local_engine: Literal["system", "melo"] | None = Field(
        default=None, description="本地朗读引擎：system 或 melo"
    )
    local_dir: str | None = Field(
        default=None, description="MeloTTS 本地模型目录"
    )


class ASRSettings(BaseModel):
    """本地语音识别设置。"""

    model_dir: str | None = None
    language: str | None = None


class UISettings(BaseModel):
    """界面设置。"""

    guided_default: bool | None = None
    locale: str | None = None
    theme: str | None = None


class RetrievalSettings(BaseModel):
    """检索参数。"""

    top_k: int | None = Field(default=None, ge=1, le=50)
    hybrid_alpha: float | None = Field(default=None, ge=0.0, le=1.0)
    min_vec_score: float | None = Field(
        default=None, ge=0.0, le=1.0, description="向量通道最低余弦阈值，低于则丢弃"
    )


class MemorySettings(BaseModel):
    """记忆总开关。"""

    enabled: bool | None = None


class PrefetchSettings(BaseModel):
    """预生成开关：讲义/练习完成后，后台提前备好下一份资源（默认开）。"""

    enabled: bool | None = None


class SettingsUpdate(BaseModel):
    """``PUT /api/settings`` 请求体（分组更新，字段均可省略）。"""

    model_config = ConfigDict(extra="forbid")

    llm: LLMSettings | None = None
    embed: EmbedSettings | None = None
    tts: TTSSettings | None = None
    asr: ASRSettings | None = None
    ui: UISettings | None = None
    retrieval: RetrievalSettings | None = None
    memory: MemorySettings | None = None
    prefetch: PrefetchSettings | None = None


class SettingsTestRequest(BaseModel):
    """``POST /api/settings/test`` 请求体。"""

    target: Literal["llm", "embed", "tts", "ollama"] = Field(description="测试对象")
