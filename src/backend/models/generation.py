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
    "MaterialOutlineCreate",
    "MaterialChapter",
    "MaterialChaptersCreate",
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


class MaterialOutlineCreate(BaseModel):
    """``POST /api/materials/outline`` 请求体：主题模式第一步（出材料目录）。

    用户没有材料、只想「打字说想学什么」时，先让 AI 把材料写出来；
    材料真正入库后，下游（大纲/讲义/练习/引用）走的还是既有链路。
    """

    topic: str = Field(min_length=1, description="想学的主题，例如「Python 装饰器」")
    level: str = Field(default="beginner", description="beginner|intermediate|advanced")
    depth: str = Field(
        default="standard", description="档位：brief=4 章 / standard=6 章 / detailed=8 章")
    chapter_count: int | None = Field(
        default=None, ge=3, le=12, description="显式章数（覆盖档位）")


class MaterialChapter(BaseModel):
    """材料目录里的一章（用户可在前端审阅后改标题/增删）。"""

    title: str = Field(min_length=1, description="章节标题")
    brief: str = Field(default="", description="本章要讲什么（一句话）")


class MaterialChaptersCreate(BaseModel):
    """``POST /api/materials/chapters`` 请求体：第二步（逐章写正文并落成材料）。"""

    generation_id: str = Field(min_length=1, description="第一步返回的 generation_id")
    chapters: list[MaterialChapter] | None = Field(
        default=None, description="用户审阅/修改后的目录；不传则用第一步的结果")
