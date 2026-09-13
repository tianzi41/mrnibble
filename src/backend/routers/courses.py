"""课程学习路由（P0：课程生成 → 白板讲义 → 随堂练习 → 进度）。

所有生成为**异步任务**：``POST`` 返回 ``job_id``，前端轮询 ``GET /api/courses/jobs/{id}``。
交互只依赖文字输入与选项（画像/引导/语音均不在 P0 范围内）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..errors import AppError, ok
from ..services.courses import CourseService
from ..services.coursemedia import get_course_media_service

router = APIRouter()

__all__ = ["router"]


class CourseCreate(BaseModel):
    """``POST /api/courses`` 请求体。"""

    goal: str = Field(min_length=1, description="学习目标（学完想做到什么）")
    document_ids: list[str] | None = Field(default=None, description="来源材料；空=全部已解析材料")
    level: str = Field(default="beginner", description="beginner|intermediate|advanced")
    depth: str = Field(default="standard", description="brief|standard|detailed")
    unit_count: int | None = Field(default=None, ge=1, le=8, description="单元数量；不填 = 由系统按材料体量自动决定")
    language: str = Field(default="zh", description="课程语言")


class OutlineUpdate(BaseModel):
    """``POST /api/courses/{id}/outline:regenerate`` 请求体（字段均可选）。"""

    goal: str | None = None
    level: str | None = None
    depth: str | None = None
    unit_count: int | None = None
    note: str | None = Field(
        default=None,
        description="自定义要求（本次重新生成的额外说明，会作为补充要求写进提示词）",
    )


class OutlineConfirm(BaseModel):
    """``POST /api/courses/{id}/outline:confirm`` 请求体（用户确认后的结构）。"""

    title: str | None = None
    units: list[dict] = Field(default_factory=list)


class LessonPatch(BaseModel):
    """``PUT /api/courses/lessons/{lid}`` 请求体（绑定课堂对话）。"""

    conversation_id: str | None = None
    complete: bool | None = Field(default=None, description="true 时标记该讲次完成")


class MarksSave(BaseModel):
    """``PUT /api/courses/lessons/{lid}/marks`` 请求体。"""

    marks: list[dict] = Field(default_factory=list)


class CoursewareSave(BaseModel):
    """``PUT /api/courses/lessons/{lid}/courseware`` 请求体。"""

    slides: list[dict] = Field(default_factory=list, description="学生课件页")
    scripts: list[dict] = Field(default_factory=list, description="讲师讲稿，按 slide_id 关联课件")


class PracticeGenerate(BaseModel):
    """``POST /api/courses/lessons/{id}/practice`` 请求体。"""

    count: int = Field(default=5, ge=1, le=20)


class GradeRequest(BaseModel):
    """``POST /api/courses/lessons/{id}/grade`` 请求体。"""

    answers: list[dict] = Field(default_factory=list, description="[{question_id, answer}]")


class CheckRequest(BaseModel):
    """``POST /api/courses/lessons/{id}/check`` 请求体（单题即时判定）。"""

    question_id: str = Field(min_length=1, description="题目 id")
    answer: Any = Field(default=None, description="选项下标（single/boolean）或文本（fill_in/open）")


def _svc() -> CourseService:
    """课程服务单例（路由层统一取用）。"""
    return CourseService.get_instance()


# ── 课程列表 / 详情 ─────────────────────────────────────
@router.get("/courses")
def list_courses(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    """列出课程（含进度）。"""
    items = _svc().list_courses(limit=limit)
    return ok({"items": items, "total": len(items)})


@router.post("/courses", summary="创建课程并启动大纲生成")
def create_course(payload: CourseCreate) -> dict:
    """创建课程并启动大纲生成任务。"""
    data = payload.model_dump()
    return ok(_svc().create_course(data))


@router.post("/courses/suggest-goals", summary="按材料推荐学习目标")
def suggest_goals(payload: GoalSuggest | None = None) -> dict:
    """根据勾选材料预测几个「学完想做到什么」的目标（创建向导点选用）。"""
    ids = (payload.document_ids if payload else None) or []
    return ok(_svc().suggest_goals(ids))


@router.get("/courses/{cid}")
def get_course(cid: str) -> dict:
    """课程详情（含单元、讲次与进度）。"""
    return ok(_svc().get_course(cid))


@router.delete("/courses/{cid}")
def delete_course(cid: str) -> dict:
    """删除课程（级联单元/讲次/题目/作答）。"""
    return ok({"deleted": _svc().delete_course(cid)})


@router.post("/courses/{cid}/outline:regenerate")
def regenerate_outline(cid: str, payload: OutlineUpdate | None = None) -> dict:
    """按新的目标/基础/体量重新生成大纲。"""
    body = payload.model_dump(exclude_none=True) if payload else None
    return ok(_svc().regenerate_outline(cid, body))


@router.post("/courses/{cid}/outline:confirm")
def confirm_outline(cid: str, payload: OutlineConfirm) -> dict:
    """确认课程结构（可含手工修改后的标题/目标）。"""
    return ok(_svc().confirm_outline(cid, payload.model_dump()))


# ── 讲次 ────────────────────────────────────────────────
@router.get("/courses/lessons/{lid}")
def get_lesson(lid: str) -> dict:
    """讲次详情（白板/引用/状态）。"""
    return ok(_svc().get_lesson(lid))


@router.put("/courses/lessons/{lid}")
def patch_lesson(lid: str, payload: LessonPatch) -> dict:
    """更新讲次：绑定课堂对话 / 标记完成。"""
    if payload.complete:
        return ok(_svc().complete_lesson(lid))
    return ok(_svc().bind_conversation(lid, payload.conversation_id or ""))


@router.post("/courses/lessons/{lid}/lecture")
def generate_lecture(lid: str) -> dict:
    """生成讲次白板讲义（异步）。"""
    return ok(_svc().generate_lecture(lid))


@router.put("/courses/lessons/{lid}/courseware")
def save_courseware(lid: str, payload: CoursewareSave) -> dict:
    """独立更新课件页与讲师讲稿。"""
    return ok(_svc().save_courseware(lid, payload.slides, payload.scripts))


# ── 材料标注（P1）──────────────────────────────────────
@router.get("/courses/lessons/{lid}/marks")
def get_marks(lid: str) -> dict:
    """读取讲次的材料标注。"""
    return ok(_svc().get_marks(lid))


@router.put("/courses/lessons/{lid}/marks")
def save_marks(lid: str, payload: MarksSave) -> dict:
    """保存材料标注（高亮 / 圈注 / 旁注）。"""
    return ok(_svc().save_marks(lid, payload.marks))


# ── 单元总结（P1）──────────────────────────────────────
@router.post("/courses/units/{uid}/summary")
def generate_unit_summary(uid: str) -> dict:
    """生成单元总结（异步）。"""
    return ok(_svc().generate_unit_summary(uid))


@router.get("/courses/units/{uid}/summary")
def get_unit_summary(uid: str) -> dict:
    """读取单元总结。"""
    return ok(_svc().get_unit_summary(uid))


# ── 导出（P1）──────────────────────────────────────────
@router.get("/courses/lessons/{lid}/export", summary="导出讲义或课堂对话（Markdown）")
def export_lesson(
    lid: str,
    kind: str = Query(default="lesson", pattern="^(lesson|conversation)$"),
) -> Response:
    """导出讲次内容：``lesson`` = 讲义+练习，``conversation`` = 课堂对话。"""
    svc = _svc()
    if kind == "conversation":
        lesson = svc.get_lesson(lid)
        cid = lesson.get("conversation_id")
        if not cid:
            raise AppError(1002, "还没有课堂对话", "先在课堂上提问，再导出")
        text = get_course_media_service().conversation_markdown(cid)
    else:
        text = get_course_media_service().lesson_markdown(svc.get_lesson(lid))
    return Response(
        content=text, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": "attachment"},
    )


# ── 资料原页（P1：PDF 渲染 / 图片题）────────────────────
@router.get("/courses/documents/{doc_id}/page", summary="材料某页渲染为 PNG")
def render_page(
    doc_id: str,
    page_no: int = Query(default=1, ge=1),
    scale: float = Query(default=2.0, ge=1.0, le=4.0),
) -> Response:
    """把 PDF 第 ``page_no`` 页渲染为 PNG（图片题用）。"""
    data = get_course_media_service().render_page(doc_id, page_no, scale)
    return Response(content=data, media_type="image/png")


@router.get("/courses/documents/{doc_id}/raw", summary="材料原始文件（PDF 标注用）")
def raw_document(doc_id: str) -> FileResponse:
    """返回材料原始 PDF（供前端 pdf.js 渲染与标注）。"""
    path, _ = get_course_media_service().raw_file(doc_id)
    return FileResponse(str(path), media_type="application/pdf",
                        filename=path.name)


# ── 练习 ────────────────────────────────────────────────
@router.post("/courses/lessons/{lid}/practice")
def generate_practice(lid: str, payload: PracticeGenerate | None = None) -> dict:
    """生成随堂练习（异步）。"""
    count = payload.count if payload else 5
    return ok(_svc().generate_practice(lid, count=count))


@router.get("/courses/lessons/{lid}/practice")
def list_practice(lid: str) -> dict:
    """列出题目（不含正确答案）。"""
    items = _svc().list_questions(lid)
    return ok({"items": items, "total": len(items)})


@router.post("/courses/lessons/{lid}/check")
def check_answer(lid: str, payload: CheckRequest) -> dict:
    """判定单题作答并返回对错与解析（不落库，供逐题即时反馈）。"""
    return ok(_svc().check_answer(lid, payload.question_id, payload.answer))


@router.post("/courses/lessons/{lid}/grade")
def grade_practice(lid: str, payload: GradeRequest) -> dict:
    """提交作答并判分（客观题确定性判分，开放题模型评分）。"""
    return ok(_svc().grade(lid, payload.answers))


# ── 任务 ────────────────────────────────────────────────
@router.get("/courses/jobs/{jid}")
def get_job(jid: str) -> dict:
    """查询生成任务（前端轮询）。"""
    return ok(_svc().get_job(jid))


@router.get("/courses/{cid}/jobs")
def list_jobs(cid: str) -> dict:
    """列出课程的生成任务。"""
    items = _svc().list_jobs(cid)
    return ok({"items": items, "total": len(items)})
