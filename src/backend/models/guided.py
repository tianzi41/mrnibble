"""引导式教学结构化输出 schema（架构文档 §8.1）。

**这是引用防伪与"首轮不给答案"两道结构性防线的载体**：

- 字段集中**不存在** ``page_no`` —— 模型没有任何写页码的通道；
- ``final_answer`` 首轮必须为空字符串，``conclusion_allowed`` 首轮必须为 ``false``，
  由服务端护栏（:mod:`backend.services.guardrails`）**校验而非信任**模型声明。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "GuidedMode",
    "NextAction",
    "DecompositionStep",
    "KnowledgeGap",
    "GuidedOutput",
]

GuidedMode = Literal["explain", "probe", "evaluate"]
NextAction = Literal["ask_follow_up", "wait_answer", "evaluate", "give_hint", "conclude"]


class DecompositionStep(BaseModel):
    """拆解步骤（首轮 ≥2 条，R-F02）。"""

    step: int = Field(ge=1, description="步骤序号，从 1 起")
    title: str = Field(min_length=1, description="步骤标题")
    hint: str = Field(default="", description="提示（不给结论）")


class KnowledgeGap(BaseModel):
    """识别到的知识盲区（R-F03）。"""

    topic: str = Field(min_length=1, description="盲区主题")
    evidence: str = Field(default="", description="判定依据（学生回答摘要）")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class GuidedOutput(BaseModel):
    """引导式教学结构化输出。"""

    mode: GuidedMode = Field(description="当前教学态：讲解/追问/评估")
    final_answer: str = Field(default="", description="最终答案；**首轮必须为空**")
    decomposition_steps: list[DecompositionStep] = Field(
        default_factory=list, min_length=0, description="拆解步骤"
    )
    follow_up_questions: list[str] = Field(
        default_factory=list, description="追问（首轮 ≥1 个问句）"
    )
    knowledge_gaps: list[KnowledgeGap] = Field(default_factory=list)
    next_action: NextAction = Field(default="ask_follow_up")
    student_state: dict[str, float] = Field(
        default_factory=dict, description="{mastery,confidence} ∈ [0,1]"
    )
    conclusion_allowed: bool = Field(
        default=False, description="本轮是否允许给出最终答案；**首轮必须为 false**"
    )
    summary: str = Field(default="", description="面向学生的引导话术（Markdown，不含最终结论）")
    citations_used: list[int] = Field(
        default_factory=list, description="引用的 [[c:N]] 编号，服务端据此回填页码"
    )

    @field_validator("follow_up_questions", mode="before")
    @classmethod
    def _coerce_questions(cls, v: Any) -> list[str]:
        """宽容处理模型把追问输出成单个字符串或对象数组的情况。"""
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        out: list[str] = []
        for item in v or []:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(str(item.get("question") or item.get("text") or "").strip())
        return [q for q in out if q]

    @field_validator("student_state", mode="before")
    @classmethod
    def _clamp_state(cls, v: Any) -> dict[str, float]:
        """把越界的掌握度/置信度夹到 [0,1]，并忽略非法键。"""
        if not isinstance(v, dict):
            return {}
        out: dict[str, float] = {}
        for k in ("mastery", "confidence"):
            try:
                out[k] = max(0.0, min(1.0, float(v.get(k, 0.0))))
            except (TypeError, ValueError):
                continue
        return out

    def is_first_turn_compliant(self) -> bool:
        """首轮合规性**结构判定**：无最终答案 + 未允许总结 + 有拆解与追问。"""
        return (
            not self.final_answer.strip()
            and not self.conclusion_allowed
            and len(self.decomposition_steps) >= 2
            and len(self.follow_up_questions) >= 1
        )

    def to_payload(self) -> dict[str, Any]:
        """转为下发前端 / 存 ``messages.content_json`` 的字典。"""
        return self.model_dump()
