"""资料生成路由（架构文档 §6.8）+ 闪卡复习接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..errors import AppError, ok
from ..models.generation import (
    GenerationCreate,
    MaterialChaptersCreate,
    MaterialOutlineCreate,
)
from ..models.memory import MemoryCreate
from ..services.flashcards import FlashcardService, REVIEW_QUALITY
from ..services.generation import GenerationService
from ..services.materials import get_material_service

router = APIRouter()

__all__ = ["router"]


# ── 生成 ────────────────────────────────────────────────
@router.post("/generations")
def create_generation(payload: GenerationCreate) -> dict:
    """创建生成任务（异步，返回 generation_id 后轮询）。"""
    svc = GenerationService.get_instance()
    return ok(svc.start(payload.type, payload.document_ids, payload.params))


@router.get("/generations")
def list_generations(
    type: str | None = Query(default=None, alias="type"),
) -> dict:
    """列出生成产物。"""
    svc = GenerationService.get_instance()
    items = svc.list(type_=type)
    return ok({"items": items, "total": len(items)})


@router.get("/generations/{gid}")
def get_generation(gid: str) -> dict:
    """读取生成产物（前端轮询 status）。"""
    gen = GenerationService.get_instance().get(gid)
    if gen is None:
        raise AppError(1001, "生成任务不存在")
    return ok(gen)


@router.delete("/generations/{gid}")
def delete_generation(gid: str) -> dict:
    """删除生成产物。"""
    deleted = GenerationService.get_instance().delete(gid)
    if not deleted:
        raise AppError(1001, "生成任务不存在")
    return ok({"deleted": True})


# ── 闪卡复习 ────────────────────────────────────────────
# ── 主题模式：无材料也能学（先让 AI 把材料写出来，再走既有课程链路）──
@router.post("/materials/outline", summary="主题模式第一步：生成材料目录")
def material_outline(payload: MaterialOutlineCreate) -> dict:
    """按主题产出「教学材料」的目录（JSON）；前端展示给用户审阅/修改后再写正文。"""
    return ok(get_material_service().start_outline(payload.model_dump(exclude_unset=True)))


@router.post("/materials/chapters", summary="主题模式第二步：逐章写正文并落成材料")
def material_chapters(payload: MaterialChaptersCreate) -> dict:
    """目录确认后逐章生成正文，落进资料库（source_type='ai'）并建索引。

    进度看 ``GET /api/generations/{gid}``：``content_md`` 里 ``## `` 的条数 = 已写章数。
    """
    return ok(get_material_service().start_chapters(payload.model_dump(exclude_unset=True)))


@router.get("/flashcards")
def list_flashcards(
    generation_id: str | None = None,
    due_only: bool = Query(default=False),
) -> dict:
    """列出闪卡（``due_only=true`` 只取到期卡）。"""
    items = FlashcardService.get_instance().list(
        generation_id=generation_id, due_only=due_only
    )
    return ok({"items": items, "total": len(items)})


@router.post("/flashcards/{cid}/review")
def review_flashcard(cid: str, payload: dict) -> dict:
    """记录一次复习（``result``: again/hard/good/easy）。

    答错（``again``）时会同步写入一条知识盲区记忆（R-F03 闭环）。
    """
    result = str(payload.get("result") or "").strip()
    card = FlashcardService.get_instance().review(cid, result)
    if result == "again":
        try:
            from ..services.memory import MemoryService

            MemoryService.get_instance().add(
                f"闪卡答错：{card['question'][:120]}",
                type_="knowledge_gap",
                source="auto",
                confidence=0.8,
                document_id=card.get("document_id"),
            )
        except Exception:  # pragma: no cover - 盲区写入失败不阻断复习
            pass
    return ok(card)


@router.delete("/flashcards/{cid}")
def delete_flashcard(cid: str) -> dict:
    """删除单张闪卡。"""
    deleted = FlashcardService.get_instance().delete(cid)
    if not deleted:
        raise AppError(1001, "闪卡不存在")
    return ok({"deleted": True})
