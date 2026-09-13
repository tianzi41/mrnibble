"""资料生成 schema（架构文档 §6.8）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "GenerationType",
    "GenerationCreate",
    "GenerationOut",
    "QuizItem",
    "FlashcardItem",
]

GenerationType = Literal["cheatsheet", "notes", "mindmap", "quiz", "flashcard"]


class GenerationCreate(BaseModel):
    """``POST /api/generations`` 请求体。"""

    type: GenerationType
    document_ids: list[str] | None = Field(default=None, description="来源材料；None=全库")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="{length: brief|standard|detailed, count: int, language: str}",
    )


class QuizItem(BaseModel):
    """练习题（R-C04：题干/选项/答案/解析四要素齐全）。"""

    stem: str = Field(min_length=1, description="题干")
    options: list[str] = Field(min_length=2, description="选项")
    answer_index: int = Field(ge=0, description="正确选项下标")
    explanation: str = Field(default="", description="解析")


class FlashcardItem(BaseModel):
    """闪卡（R-C05：正/反面）。"""

    question: str = Field(min_length=1, description="正面（问题）")
    answer: str = Field(min_length=1, description="背面（答案）")


class GenerationOut(BaseModel):
    """生成产物对象。"""

    id: str
    type: str
    title: str | None = None
    document_ids: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    content_md: str | None = None
    content_json: dict[str, Any] | None = None
    collection: str | None = None
    status: str = "running"
    error: str | None = None
    model: str | None = None
    created_at: str = ""
    updated_at: str = ""
