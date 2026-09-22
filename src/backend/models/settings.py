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
    "PrefetchSettings",
    "ProfileSettings",
    "GuideSettings",
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
    mode: Literal["off", "local", "cloud", "custom"] | None = None
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
    # ── 自定义 HTTP 语音服务（本地部署的 TTS 项目，自定义协议）──
    custom_url: str | None = Field(
        default=None, description="地址模板，含 {text} 占位符"
    )
    custom_method: Literal["GET", "POST"] | None = Field(
        default=None, description="请求方法：GET 或 POST"
    )
    custom_body: str | None = Field(
        default=None, description="POST 的 body 模板（JSON 文本，含 {text}）"
    )
    custom_format: str | None = Field(
        default=None, description="返回音频格式（wav / mp3）"
    )
    custom_timeout: int | None = Field(
        default=None, description="读超时秒数（本地模型可能很慢）"
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


class ProfileSettings(BaseModel):
    """用户画像（新手引导收集；每次 AI 生成时作为上下文注入）。

    存选项 id（与前端 js/profile_fields.js 的 FIELDS 一一对应）；
    空串 / 不传 = 未填（注入时跳过该项）。
    """

    age: str | None = None
    role: str | None = None
    stage: str | None = None
    grade: str | None = None
    purpose: str | None = None
    style: str | None = None
    daily: str | None = None
    fields: str | None = None      # 常学领域，逗号分隔（多选）
    note: str | None = None        # 开放题一句话


class GuideSettings(BaseModel):
    """新手引导状态（首次启动三步向导）。"""

    done: bool | None = None       # 完成标志
    step: str | None = None        # 断点：intro / profile / api


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
    profile: ProfileSettings | None = None
    guide: GuideSettings | None = None


class SettingsTestRequest(BaseModel):
    """``POST /api/settings/test`` 请求体。"""

    target: Literal["llm", "embed", "tts", "ollama"] = Field(description="测试对象")
