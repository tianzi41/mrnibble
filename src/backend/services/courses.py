"""课程学习服务（P0：把材料变成一门能学完的课）。

覆盖的闭环::

    创建课程（目标/基础/体量） → 大纲生成任务（可展示阶段） → 用户确认结构
    → 讲次白板讲义 → 随堂练习 → 判分 → 进度

设计要点（与既有模块保持一致）：

- **引用防线不变**：模型只能输出 ``[[c:N]]``，页码由 :func:`citations.build_context`
  生成的引用表按 ``chunk_id`` 回填，课程讲义与练习题都不例外；
- **全部生成为异步任务**：``POST`` 立即返回 ``job_id``，前端轮询
  ``GET /api/courses/jobs/{id}``，避免长请求超时；
- **三段降级**：模型调用失败 / 返回非法 JSON / 结构校验不过，一律先重试一次，
  仍失败则走**确定性兜底模板**（内容取自真实材料片段），保证课程不会空着；
- **交互只依赖文字与选项**：本模块不引入任何语音/画像/引导式依赖（P0 范围外）。

对外入口见 :class:`CourseService`。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import traceback
from pathlib import Path
from typing import Any, Sequence

from ..db.connection import get_db
from ..errors import AppError
from ..utils.ids import new_id
from ..utils.timeutil import now_iso
from .citations import build_context, resolve_citations
from .llm import LLMClient, extract_json_object
from .retrieval import get_retrieval_service

logger = logging.getLogger(__name__)

__all__ = ["CourseService", "get_course_service", "LEVELS", "DEPTHS"]

LEVELS: tuple[str, ...] = ("beginner", "intermediate", "advanced")
DEPTHS: tuple[str, ...] = ("brief", "standard", "detailed")

_LEVEL_NAME = {"beginner": "零基础", "intermediate": "有基础", "advanced": "进阶"}
_DEPTH_NAME = {"brief": "概览", "standard": "标准", "detailed": "深入"}
_KIND_NAME = {"lecture": "讲解", "practice": "练习", "project": "项目"}

# 生成时注入的材料片段上限（控制 prompt 长度）。
_MAX_HITS = 8
# 结构校验失败后的自动重试次数。
_MAX_RETRY = 1
# 大纲默认单元数 / 每单元讲次数（模型输出不足时用于兜底）。
_DEFAULT_UNITS = 3
_DEFAULT_LESSONS = 3

# ── 通用规则（所有课程生成提示词共用）───────────────────
_BASE_RULES = """通用规则：
1. **只依据下方 [材料N]** 组织内容，引用一律写 [[c:编号]]（编号取自 [材料N] 的 N）；禁止自己书写页码、章节号或文件名。
2. 材料里没有的内容不要编造；确需补充时以「（材料外补充）」开头。
3. 只输出一个 JSON 对象，不要输出 JSON 以外的任何文字（包括解释与代码块标记）。"""

_UNIT_SUMMARY_PROMPT = """你是学习教练。这个单元已经学完，请根据「学过的内容」与「练习表现」写一份单元总结。

输出 JSON：
{"recap":"本单元学了什么（3~5 句）",
 "mastered":["已经掌握的点"],
 "weak_points":["还需要巩固的点"],
 "next_steps":["下一步建议"],
 "score_note":"对本次练习表现的一句话评价"}

要求：mastered / weak_points / next_steps 各 2~4 条，都要落到具体内容，不要写空话；
weak_points 必须来自练习中真实答错或得分偏低的题目。
""" + _BASE_RULES

_OUTLINE_PROMPT = """你是课程设计师。请根据用户的学习目标与材料，设计一门可学完的课程大纲。

要求：
- 课程标题：一句话点明主题；
- __UNITS_RULE__；
- 每个单元最后一个讲次是 ``practice``（随堂练习），其余为 ``lecture``；
- ``objective`` 写「学完这一节能做到什么」，不要写成章节名；
- 讲次顺序必须由浅入深，覆盖材料的主要内容。

输出 JSON：
{"title":"课程标题","summary":"课程简介（2~3 句）",
 "units":[{"title":"单元标题","summary":"单元简介",
 "lessons":[{"title":"讲次标题","objective":"学习目标","kind":"lecture|practice","depth":"establish|define|derive|apply"}]}]}
""" + _BASE_RULES

# 学习目标推荐（创建向导里的「帮我推荐」按钮）。
_GOAL_PROMPT = """你是学习规划师。用户挑了几份学习材料想开一门课，请根据材料内容预测 4 个「学完想做到什么」的学习目标。

要求：
- 每条一句话，以「能」开头，具体、可检验（不要写「了解/熟悉」这类模糊词）；
- 4 条侧重不同：入门理解、方法运用、综合应用、易错辨析各占一条；
- 只依据材料真实覆盖的内容，不要编造材料里没有的主题。

输出 JSON：{"goals":["目标1","目标2","目标3","目标4"]}
只输出这一个 JSON 对象，不要输出 JSON 以外的任何文字（包括解释与代码块标记）。
"""

_LECTURE_PROMPT = """你是课堂讲师。请为下面这一讲准备一版**课堂内容包**，__DEPTH__。

讲次：__TITLE__
目标：__OBJECTIVE__
所属单元：__UNIT__

必须把三类内容分清楚：
1. slides：学生看的课件页，短句、要点、对比、例子，不堆完整讲稿；
2. scripts：讲师朗读/讲述的讲稿，按 slide_id 关联课件，围绕当前页解释、举例、衔接，不能只是复述课件；
3. cards：讲义全文，供学生课后通读复习，可比 slides 更完整。

输出 JSON：
{"summary":"本讲一句话导览",
 "slides":[{"id":"slide-1","kind":"concept|example|formula|quote|note","title":"课件页标题","bullets":["短要点1","短要点2"],"body":"可选补充短句，可含 [[c:N]]","citation_refs":[1]}],
 "scripts":[{"slide_id":"slide-1","text":"讲师实际朗读的完整讲稿，可含 [[c:N]]。要解释课件、补充上下文和自然转场。"}],
 "cards":[{"kind":"concept|example|formula|quote|note","title":"讲义卡片标题","body":"讲义正文，可含 [[c:N]]"}],
 "outline":["要点1","要点2"],
 "keypoints":[{"term":"术语/公式","desc":"解释"}],
 "recap":"本讲回顾（3 句以内）",
 "marks":[{"n":1,"kind":"highlight","text":"这句为什么重要"}]}

要求：
- slides 至少 3 页；每页只放学生需要看的标题、短要点或例子，避免完整讲稿；
- scripts 必须与 slides 逐页一一对应，slide_id 必须来自 slides；每段 script 应比对应 slide 更口语、更完整；
- cards 至少 3 张，其中至少 1 张 kind 为 quote（直接引用材料原句）；keypoints 至少 2 条；不要输出空数组；
- slides/scripts/cards 中如引用材料，统一使用 [[c:N]]。citation_refs 只写本页用到的材料编号，不写页码。

marks 是**材料标注意图**：n 必须是本讲引用到的材料编号（即你在内容里用过的
[[c:N]] 的 N），kind 取 highlight（黄底高亮）或 circle（红圈），text 是要在旁边写的一句话旁注。
没有把握就返回空数组，不要编造 n。
""" + _BASE_RULES

_PRACTICE_PROMPT = """你是出题老师。请围绕这一讲出 __COUNT__ 道题，__DEPTH__。

讲次：__TITLE__
目标：__OBJECTIVE__

输出 JSON：
{"items":[{"type":"single","stem":"题干","options":["A","B","C","D"],"answer":0,"explanation":"解析"},
          {"type":"boolean","stem":"判断题干","options":["正确","错误"],"answer":1,"explanation":"解析"},
          {"type":"fill_in","stem":"填空题（用 ____ 表示待填）","answer":["标准答案","同义答案"],"explanation":"解析"},
          {"type":"open","stem":"开放题","answer":"参考答案","explanation":"评分要点"},
          {"type":"single","stem":"看图题：图中所示的结论是什么","options":["A","B","C","D"],"answer":0,
           "explanation":"解析","image":{"n":1}}]}

要求：
- **题型配比以单选为主**：5 题时 3~4 道 single，最多 1 道 boolean、最多 1 道 fill_in、最多 1 道 open；
  题量更少时优先保证 single；开放题只在确实需要展开论述时使用；
- 单选题 answer 是正确选项的**下标**（0 起）；判断题 options 固定 ["正确","错误"]，answer 为 0 或 1；
- fill_in 的 answer 是**可接受答案数组**（可含同义写法）；
- open 的 answer 是参考答案文本；
- **图片题**：需要看图时加 "image":{"n":材料编号}，n 必须是你引用过的材料编号，
  系统会把材料对应页渲染成图；最多出 1 道图片题，没有合适的图就不要加 image 字段；
- 每题都要有 explanation；全部题目必须来自本讲内容。
""" + _BASE_RULES

_GRADE_PROMPT = """你是阅卷老师。请为学生的开放题作答评分。

题目：__STEM__
参考答案：__ANSWER__
学生作答：__RESPONSE__

只输出 JSON：{"correct":true|false,"score":0~1,"feedback":"针对这份作答的点评（2~3 句）"}
评分标准：核心意思答出且无明显错误 → correct=true 且 score≥0.6；
部分正确 → correct=false 且 score 0.3~0.5；答非所问或空白 → score=0。
"""

_DEPTH_HINT = {
    "brief": "控制在最核心的内容，讲解精炼",
    "standard": "篇幅适中，讲清概念并给出例子",
    "detailed": "详尽展开，含推导、易错点与更多例子",
}

# 填空题归一化：去空白、去句末标点、全角转半角、英文小写。
_PUNCT = re.compile(r"[\s，。；、．,.;:：!！?？\"'“”‘’()（）\[\]【】]+")


def _norm_text(s: str) -> str:
    """填空题判分用的归一化（空白/标点/大小写不敏感）。"""
    s = (s or "").strip()
    s = s.replace("　", " ")
    s = _PUNCT.sub("", s)
    return s.lower()


def _json_loads(raw: str | None, default: Any) -> Any:
    """安全解析 JSON 列（损坏时返回默认值，不让读取链路中断）。"""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _err_text(exc: BaseException) -> str:
    """把后台任务异常转成可读文案。

    后台线程里抛出的异常如果不带位置信息，前端只能看到「生成失败」，
    排查成本很高。这里带上**最深一层调用帧**（文件:行号:函数），
    既不泄露堆栈全貌，又足够定位问题。
    """
    message = str(getattr(exc, "message", "") or "").strip()
    detail = f"{type(exc).__name__}: {exc}".strip()
    frames = traceback.extract_tb(exc.__traceback__)
    where = ""
    if frames:
        last = frames[-1]
        where = f" @ {Path(last.filename).name}:{last.lineno} in {last.name}"
    return ((message or detail) + where)[:500]


def _row_get(row: Any, column: str, default: Any = None) -> Any:
    """读取行字段；迁移前没有该列时返回默认值（不抛 ``IndexError``/``KeyError``）。"""
    try:
        return row[column]
    except (IndexError, KeyError, TypeError):
        return default


def _clamp01(value: Any) -> float:
    """把标注坐标裁剪到 ``[0,1]``（画布相对坐标）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, num))


def _column_available(row: Any, column: str) -> bool:
    """``sqlite3.Row`` 是否含某列（迁移前后的列差异不应导致 KeyError）。"""
    try:
        keys = row.keys()
    except AttributeError:
        return True
    return column in keys


class CourseService:
    """课程编排（进程级单例）。"""

    _instance: "CourseService | None" = None

    @classmethod
    def get_instance(cls) -> "CourseService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ══════════════════════════════════════════════════
    # 课程列表 / 详情 / 进度
    # ══════════════════════════════════════════════════
    def list_courses(self, limit: int = 100) -> list[dict[str, Any]]:
        """列出课程（含进度摘要）。"""
        db = get_db()
        rows = db.query_all(
            "SELECT * FROM courses ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return [self._course_out(r, with_progress=True) for r in rows]

    def get_course(self, cid: str) -> dict[str, Any]:
        """读取课程详情（含单元、讲次与进度）。"""
        db = get_db()
        row = db.query_one("SELECT * FROM courses WHERE id = ?", (cid,))
        if row is None:
            raise AppError(1001, "课程不存在")
        return self._course_out(row, with_units=True, with_progress=True)

    def get_lesson(self, lesson_id: str) -> dict[str, Any]:
        """读取单个讲次（含白板、引用与课程定位）。"""
        db = get_db()
        row = db.query_one("SELECT * FROM course_lessons WHERE id = ?", (lesson_id,))
        if row is None:
            raise AppError(1001, "讲次不存在")
        return self._lesson_out(row)

    def delete_course(self, cid: str) -> bool:
        """删除课程（级联单元/讲次/题目/作答）。"""
        cur = get_db().execute("DELETE FROM courses WHERE id = ?", (cid,))
        return cur.rowcount > 0

    @staticmethod
    def _progress(cid: str) -> dict[str, Any]:
        """计算课程进度：讲次完成数与当前应学的讲次。"""
        db = get_db()
        rows = db.query_all(
            "SELECT id, unit_id, kind, status, global_ordinal FROM course_lessons"
            " WHERE course_id = ? ORDER BY global_ordinal",
            (cid,),
        )
        total = len(rows)
        done = sum(1 for r in rows if r["status"] == "done")
        current = next(
            (r["id"] for r in rows if r["status"] != "done"),
            rows[-1]["id"] if rows else None,
        )
        pct = round(done * 100 / total, 1) if total else 0.0
        return {
            "total_lessons": total,
            "done_lessons": done,
            "percent": pct,
            "current_lesson_id": current,
        }

    # ══════════════════════════════════════════════════
    # 创建课程 → 大纲生成任务
    # ══════════════════════════════════════════════════
    def create_course(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建课程并启动大纲生成任务。

        Returns:
            ``{course_id, job_id, status}``；前端据此轮询任务与课程。
        """
        llm = LLMClient.get_instance()
        llm.ensure_configured()

        document_ids = [d for d in (payload.get("document_ids") or []) if d]
        if not document_ids:
            # 没有显式选材料时退化为「全库已就绪材料」，避免空课程。
            document_ids = self._ready_document_ids()
        if not document_ids:
            raise AppError(1002, "没有可用材料", "请先上传并解析完成至少一份文档")

        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise AppError(1000, "请填写学习目标", "用一句话说明「学完想做到什么」")

        level = str(payload.get("level") or "beginner")
        if level not in LEVELS:
            level = "beginner"
        depth = str(payload.get("depth") or "standard")
        if depth not in DEPTHS:
            depth = "standard"
        # unit_count：0 = 自动（用户没选，由模型按材料体量决定）。
        try:
            raw_units = int(payload.get("unit_count") or 0)
        except (TypeError, ValueError):
            raw_units = 0
        unit_count = raw_units if raw_units >= 1 else 0

        db = get_db()
        cid = new_id()
        ts = now_iso()
        # 标题先用「目标首句」，大纲生成后由模型给出的标题覆盖。
        title = self._draft_title(goal, document_ids)
        db.execute(
            "INSERT INTO courses(id,title,goal,level,depth,unit_count,language,summary,"
            "outline_json,status,error,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, title, goal[:1000], level, depth, unit_count,
             str(payload.get("language") or "zh"), None, None, "drafting", None, ts, ts),
        )
        for did in document_ids:
            db.execute(
                "INSERT OR IGNORE INTO course_documents(course_id, document_id)"
                " VALUES (?, ?)", (cid, did),
            )

        job_id = self._new_job(cid, None, "outline", "正在查看材料")
        t = threading.Thread(
            target=self._run_outline,
            args=(cid, job_id, document_ids, goal, level, depth, unit_count),
            daemon=True,
        )
        t.start()
        return {"course_id": cid, "job_id": job_id, "status": "running"}

    def regenerate_outline(
        self, cid: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """用新的目标/基础/体量重新生成大纲（保留课程 id 与材料）。"""
        db = get_db()
        row = db.query_one("SELECT * FROM courses WHERE id = ?", (cid,))
        if row is None:
            raise AppError(1001, "课程不存在")
        payload = payload or {}
        goal = str(payload.get("goal") or row["goal"] or "").strip()
        level = str(payload.get("level") or row["level"])
        depth = str(payload.get("depth") or row["depth"])
        try:
            unit_count = int(payload.get("unit_count") or row["unit_count"])
        except (TypeError, ValueError):
            unit_count = int(row["unit_count"] or _DEFAULT_UNITS)
        # 自定义要求：用户在「重新生成」前填写的额外说明，直接作为补充要求进提示词。
        note = str(payload.get("note") or "").strip()[:1000]

        db.execute(
            "UPDATE courses SET goal=?, level=?, depth=?, unit_count=?, status='drafting',"
            " error=NULL, updated_at=? WHERE id=?",
            (goal[:1000], level, depth, unit_count, now_iso(), cid),
        )
        document_ids = self._course_document_ids(cid)
        job_id = self._new_job(cid, None, "outline", "正在查看材料")
        t = threading.Thread(
            target=self._run_outline,
            args=(cid, job_id, document_ids, goal, level, depth, unit_count, note),
            daemon=True,
        )
        t.start()
        return {"course_id": cid, "job_id": job_id, "status": "running"}

    # ── 学习目标推荐（创建向导「帮我推荐」按钮）─────────
    def suggest_goals(self, document_ids: Sequence[str]) -> dict[str, Any]:
        """根据材料预测几个学习目标（同步接口，供创建向导点选）。

        模型失败/返回非法 JSON 时走**确定性兜底**（用材料的章节/标题拼目标），
        保证按钮永远有东西可选，而不是报错把用户挡在创建流程外。
        """
        llm = LLMClient.get_instance()
        llm.ensure_configured()

        ids = [d for d in (document_ids or []) if d]
        if not ids:
            ids = self._ready_document_ids()
        if not ids:
            raise AppError(1002, "没有可用材料", "请先上传并解析完成至少一份文档")

        hits, context, _ = self._material(ids, "材料主题与核心内容", top_k=_MAX_HITS)
        if not hits:
            raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")

        goals: list[str] = []
        try:
            messages = [
                {"role": "system", "content": _GOAL_PROMPT},
                {"role": "user", "content": f"【学习材料检索结果】\n{context}"},
            ]
            parsed = self._safe_json(self._chat(messages, max_tokens=800))
            raw_goals = parsed.get("goals") if isinstance(parsed, dict) else None
            if isinstance(raw_goals, list):
                goals = [str(g).strip()[:200] for g in raw_goals if str(g).strip()][:6]
        except Exception as exc:  # noqa: BLE001 - 推荐失败走兜底，不挡创建流程
            logger.warning("学习目标推荐失败", extra={"extra_fields": {"type": type(exc).__name__}})

        if not goals:
            # 兜底：用材料章节/文档标题拼出可点选的目标
            topics: list[str] = []
            for h in hits:
                t = (h.get("section") or h.get("document_title") or "").strip()
                if t and t not in topics:
                    topics.append(t)
            goals = [f"能用自己的话讲清「{t}」的核心内容" for t in topics[:4]]
            if not goals:
                goals = ["能掌握所选材料的核心概念并完成配套练习"]
        return {"goals": goals}

    # ── 后台：大纲生成 ──────────────────────────────────
    def _run_outline(
        self,
        cid: str,
        job_id: str,
        document_ids: list[str],
        goal: str,
        level: str,
        depth: str,
        unit_count: int,
        note: str = "",
    ) -> None:
        """后台线程：检索材料 → 生成大纲 → 校验 → 落库（失败走兜底模板）。"""
        db = get_db()
        try:
            hits, context, table = self._material(document_ids, goal, top_k=_MAX_HITS)
            if not hits:
                raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")

            self._set_stage(job_id, "正在构思初步思路")
            self._set_stage(job_id, "正在构建课程结构")

            # 单元数：0 = 自动（由模型按材料体量决定），>0 = 固定个数。
            units_rule = (
                f"单元数固定为 {unit_count} 个，每个单元 2~4 个讲次" if unit_count
                else "单元数量由你根据材料体量与学习目标自行决定（通常 2~5 个），每个单元 2~4 个讲次"
            )
            prompt = _OUTLINE_PROMPT.replace("__UNITS_RULE__", units_rule) + (
                f"\n补充要求：学习者当前水平为「{_LEVEL_NAME.get(level, level)}」，"
                f"内容深度要求「{_DEPTH_NAME.get(depth, depth)}」。"
            )
            # 用户自定义要求（「重新生成大纲」时填写）：优先级高于上面的默认倾向，
            # 明确告诉模型「必须优先满足」，否则容易被通用规则淹没。
            if note:
                prompt += (
                    "\n\n【用户本次的额外要求（必须优先满足）】\n" + note
                    + "\n请在满足上述要求的前提下组织单元与讲次，"
                    "但不得偏离 [材料N] 的真实内容，也不得编造材料中没有的主题。"
                )
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": self._material_user(goal, context)},
            ]

            obj: dict[str, Any] | None = None
            for _ in range(_MAX_RETRY + 1):
                raw = self._chat(messages, max_tokens=4096)
                parsed = self._safe_json(raw)
                obj = parsed if parsed is not None else self._outline_fallback(hits, unit_count)
                validated = self._validate_outline(obj, unit_count)
                if validated is not None:
                    obj = validated
                    break
                obj = None
                messages += [
                    {"role": "assistant", "content": (raw or "")[:1500]},
                    {"role": "user", "content":
                        "上一次输出不符合 JSON 结构要求，请严格按 schema 重新输出，只输出一个 JSON 对象。"},
                ]
            if obj is None:
                obj = self._outline_fallback(hits, unit_count)
                self._set_stage(job_id, "模型输出不稳定，已按材料结构生成大纲")

            self._set_stage(job_id, "正在生成课程细节")
            self._persist_outline(cid, obj, document_ids)
            self._finish_job(job_id)
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.warning("大纲生成失败", extra={"extra_fields": {"type": type(exc).__name__}})
            message = _err_text(exc)
            self._fail_job(job_id, message)
            self._fail_course(cid, message)

    @staticmethod
    def _validate_outline(obj: dict[str, Any], unit_count: int) -> dict[str, Any] | None:
        """校验并归一化大纲结构；不合规返回 ``None``。"""
        if not isinstance(obj, dict):
            return None
        title = str(obj.get("title") or "").strip()
        units_raw = obj.get("units")
        if not title or not isinstance(units_raw, list) or not units_raw:
            return None

        units: list[dict[str, Any]] = []
        # 0 = 自动：最多保留 8 个单元（与路由上限一致）；>0 = 按用户指定截断。
        limit = unit_count if unit_count >= 1 else 8
        for u in units_raw[: limit]:
            if not isinstance(u, dict):
                continue
            u_title = str(u.get("title") or "").strip()
            lessons_raw = u.get("lessons")
            if not u_title or not isinstance(lessons_raw, list) or not lessons_raw:
                continue
            lessons: list[dict[str, Any]] = []
            for l in lessons_raw[:6]:
                if not isinstance(l, dict):
                    continue
                l_title = str(l.get("title") or "").strip()
                if not l_title:
                    continue
                kind = str(l.get("kind") or "lecture").strip().lower()
                if kind not in ("lecture", "practice", "project"):
                    kind = "lecture"
                lessons.append({
                    "title": l_title[:120],
                    "objective": str(l.get("objective") or "").strip()[:300],
                    "kind": kind,
                    "depth": str(l.get("depth") or "standard").strip()[:32],
                })
            if lessons:
                units.append({
                    "title": u_title[:120],
                    "summary": str(u.get("summary") or "").strip()[:300],
                    "lessons": lessons,
                })
        if not units:
            return None
        return {
            "title": title[:120],
            "summary": str(obj.get("summary") or "").strip()[:500],
            "units": units,
        }

    def _outline_fallback(self, hits: list[dict[str, Any]], unit_count: int) -> dict[str, Any]:
        """确定性兜底大纲：单元标题取自材料章节，讲次覆盖主要片段。"""
        sections: list[str] = []
        for h in hits:
            sec = (h.get("section") or "").strip()
            if sec and sec not in sections:
                sections.append(sec)
        want = unit_count if unit_count >= 1 else _DEFAULT_UNITS
        titles = [s for s in sections[:want]]
        while len(titles) < want:
            titles.append(f"第 {len(titles) + 1} 单元")

        units: list[dict[str, Any]] = []
        per_unit = max(2, min(4, _DEFAULT_LESSONS))
        for i, u_title in enumerate(titles):
            lessons: list[dict[str, Any]] = []
            for j in range(per_unit):
                is_last = j == per_unit - 1
                hit = hits[(i * per_unit + j) % len(hits)] if hits else {}
                snippet = str(hit.get("snippet") or "").strip().replace("\n", " ")
                lessons.append({
                    "title": f"{u_title} · 第 {j + 1} 讲" if not is_last else f"{u_title} · 随堂练习",
                    "objective": f"掌握与「{snippet[:24] or u_title}」相关的核心内容",
                    "kind": "practice" if is_last else "lecture",
                    "depth": "apply" if is_last else "define",
                })
            units.append({"title": u_title, "summary": "", "lessons": lessons})
        return {
            "title": "由材料生成的课程",
            "summary": "（模型未返回合规大纲，已按材料结构生成，可在确认前修改）",
            "units": units,
        }

    def _persist_outline(
        self, cid: str, obj: dict[str, Any], document_ids: list[str]
    ) -> None:
        """把大纲写入单元/讲次表，并把课程置为 ``ready``。"""
        db = get_db()
        ts = now_iso()
        with db.transaction():
            db.execute("DELETE FROM course_units WHERE course_id = ?", (cid,))
            global_ordinal = 0
            for u_idx, unit in enumerate(obj["units"], start=1):
                uid = new_id()
                db.execute(
                    "INSERT INTO course_units(id,course_id,ordinal,title,summary,status,created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (uid, cid, u_idx, unit["title"], unit.get("summary") or "",
                     "active" if u_idx == 1 else "pending", ts),
                )
                for l_idx, lesson in enumerate(unit["lessons"], start=1):
                    global_ordinal += 1
                    db.execute(
                        "INSERT INTO course_lessons(id,course_id,unit_id,ordinal,global_ordinal,"
                        "kind,title,objective,depth,status,created_at,updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            new_id(), cid, uid, l_idx, global_ordinal,
                            lesson["kind"], lesson["title"], lesson.get("objective") or "",
                            lesson.get("depth") or "standard", "pending", ts, ts,
                        ),
                    )
            db.execute(
                "UPDATE courses SET title=?, summary=?, outline_json=?, status='ready',"
                " error=NULL, updated_at=? WHERE id=?",
                (obj["title"], obj.get("summary") or "", json.dumps(obj, ensure_ascii=False),
                 ts, cid),
            )
        for did in document_ids:
            db.execute(
                "INSERT OR IGNORE INTO course_documents(course_id, document_id) VALUES (?, ?)",
                (cid, did),
            )

    def confirm_outline(self, cid: str, outline: dict[str, Any]) -> dict[str, Any]:
        """确认（可能已手工修改过的）课程结构，落库并置为 ``ready``。

        修改只覆盖**标题/简介/目标/类型**这些结构字段；讲次的白板与题目仍按
        ``id`` 保留，避免确认一次就把已生成的内容清空。
        """
        db = get_db()
        row = db.query_one("SELECT * FROM courses WHERE id = ?", (cid,))
        if row is None:
            raise AppError(1001, "课程不存在")

        units = outline.get("units") if isinstance(outline, dict) else None
        if not isinstance(units, list) or not units:
            raise AppError(1000, "课程结构不完整", "至少需要一个单元与一个讲次")

        ts = now_iso()
        with db.transaction():
            for u_idx, unit in enumerate(units, start=1):
                if not isinstance(unit, dict):
                    continue
                u_title = str(unit.get("title") or "").strip()[:120]
                if not u_title:
                    continue
                lessons_raw = unit.get("lessons") or []
                if not isinstance(lessons_raw, list):
                    continue
                # 优先沿用原单元（保留其下的讲次与已生成内容），不足时新建。
                u_row = db.query_one(
                    "SELECT id FROM course_units WHERE course_id = ? AND ordinal = ?",
                    (cid, u_idx),
                )
                if u_row is None:
                    uid = new_id()
                    db.execute(
                        "INSERT INTO course_units(id,course_id,ordinal,title,summary,status,"
                        "created_at) VALUES (?,?,?,?,?,?,?)",
                        (uid, cid, u_idx, u_title, str(unit.get("summary") or "").strip()[:300],
                         "active" if u_idx == 1 else "pending", ts),
                    )
                else:
                    uid = u_row["id"]
                    db.execute(
                        "UPDATE course_units SET title=?, summary=? WHERE id=?",
                        (u_title, str(unit.get("summary") or "").strip()[:300], uid),
                    )

                for l_idx, lesson in enumerate(lessons_raw[:12], start=1):
                    if not isinstance(lesson, dict):
                        continue
                    l_title = str(lesson.get("title") or "").strip()[:120]
                    if not l_title:
                        continue
                    kind = str(lesson.get("kind") or "lecture").strip().lower()
                    if kind not in ("lecture", "practice", "project"):
                        kind = "lecture"
                    l_row = db.query_one(
                        "SELECT id FROM course_lessons WHERE unit_id = ? AND ordinal = ?",
                        (uid, l_idx),
                    )
                    objective = str(lesson.get("objective") or "").strip()[:300]
                    if l_row is None:
                        db.execute(
                            "INSERT INTO course_lessons(id,course_id,unit_id,ordinal,"
                            "global_ordinal,kind,title,objective,depth,status,created_at,"
                            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (new_id(), cid, uid, l_idx, u_idx * 100 + l_idx, kind, l_title,
                             objective, str(lesson.get("depth") or "standard"), "pending",
                             ts, ts),
                        )
                    else:
                        db.execute(
                            "UPDATE course_lessons SET title=?, objective=?, kind=?, updated_at=?"
                            " WHERE id=?",
                            (l_title, objective, kind, ts, l_row["id"]),
                        )
                # 删除本单元多余的旧讲次
                db.execute(
                    "DELETE FROM course_lessons WHERE unit_id = ? AND ordinal > ?",
                    (uid, min(len(lessons_raw), 12)),
                )
            # 删除多余的旧单元
            db.execute("DELETE FROM course_units WHERE course_id = ? AND ordinal > ?",
                       (cid, len(units)))
            self._renumber(cid)
            db.execute(
                "UPDATE courses SET title=?, status='ready', error=NULL, updated_at=? WHERE id=?",
                (str(outline.get("title") or row["title"]).strip()[:120] or row["title"],
                 ts, cid),
            )
        return self.get_course(cid)

    @staticmethod
    def _renumber(cid: str) -> None:
        """重排讲次的全局序号（决定学习顺序）。"""
        db = get_db()
        rows = db.query_all(
            "SELECT l.id AS id FROM course_lessons l JOIN course_units u ON u.id = l.unit_id"
            " WHERE l.course_id = ? ORDER BY u.ordinal, l.ordinal",
            (cid,),
        )
        for i, r in enumerate(rows, start=1):
            db.execute("UPDATE course_lessons SET global_ordinal=? WHERE id=?", (i, r["id"]))

    # ══════════════════════════════════════════════════
    # 材料标注（P1：高亮 / 圈注 / 连线旁注）
    # ══════════════════════════════════════════════════
    def get_marks(self, lesson_id: str) -> dict[str, Any]:
        """读取讲次的材料标注（模型生成 + 用户手绘）。"""
        row = get_db().query_one(
            "SELECT board_marks FROM course_lessons WHERE id = ?", (lesson_id,)
        )
        if row is None:
            raise AppError(1001, "讲次不存在")
        marks = _json_loads(_row_get(row, "board_marks"), [])
        return {"lesson_id": lesson_id, "marks": marks if isinstance(marks, list) else []}

    def save_marks(self, lesson_id: str, marks: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """覆盖保存材料标注。

        只保留**能定位到具体材料页**的标注（``document_id`` + ``page_no`` 必填），
        并限制坐标在 0~1 之间，避免前端拿到越界值画到画布外。
        """
        self._require_lesson(lesson_id)
        cleaned: list[dict[str, Any]] = []
        for mk in list(marks or [])[:60]:
            if not isinstance(mk, dict):
                continue
            did = str(mk.get("document_id") or "").strip()
            try:
                page_no = int(mk.get("page_no") or 0)
            except (TypeError, ValueError):
                continue
            if not did or page_no <= 0:
                continue
            kind = str(mk.get("kind") or "highlight").strip().lower()
            if kind not in ("highlight", "circle"):
                kind = "highlight"
            cleaned.append({
                "document_id": did,
                "page_no": page_no,
                "kind": kind,
                "x": _clamp01(mk.get("x")),
                "y": _clamp01(mk.get("y")),
                "w": _clamp01(mk.get("w")),
                "h": _clamp01(mk.get("h")),
                "text": str(mk.get("text") or "").strip()[:200],
                "source": "user" if str(mk.get("source") or "user") != "model" else "model",
            })
        get_db().execute(
            "UPDATE course_lessons SET board_marks=?, updated_at=? WHERE id=?",
            (json.dumps(cleaned, ensure_ascii=False), now_iso(), lesson_id),
        )
        return {"lesson_id": lesson_id, "marks": cleaned}

    # ══════════════════════════════════════════════════
    # 单元总结（P1：学完后的回顾、薄弱点与下一步）
    # ══════════════════════════════════════════════════
    def generate_unit_summary(self, unit_id: str) -> dict[str, Any]:
        """生成单元总结（异步）。"""
        llm = LLMClient.get_instance()
        llm.ensure_configured()
        db = get_db()
        row = db.query_one("SELECT * FROM course_units WHERE id = ?", (unit_id,))
        if row is None:
            raise AppError(1001, "单元不存在")
        done = db.query_one(
            "SELECT COUNT(*) AS n FROM course_lessons WHERE unit_id = ? AND status='done'",
            (unit_id,),
        )
        if not int((done or {"n": 0})["n"] or 0):
            raise AppError(1002, "本单元还没有学完任何讲次", "先学完至少一节再生成总结")
        job_id = self._new_job(row["course_id"], None, "unit_summary", "正在整理单元总结")
        db.execute(
            "UPDATE course_units SET summary_status='running', summary_error=NULL WHERE id=?",
            (unit_id,),
        )
        t = threading.Thread(
            target=self._run_unit_summary, args=(unit_id, job_id), daemon=True
        )
        t.start()
        return {"unit_id": unit_id, "job_id": job_id, "status": "running"}

    def _run_unit_summary(self, unit_id: str, job_id: str) -> None:
        """后台线程：汇总讲次与练习表现 → 生成总结 → 落库。"""
        db = get_db()
        try:
            row = db.query_one("SELECT * FROM course_units WHERE id = ?", (unit_id,))
            if row is None:
                raise AppError(1001, "单元不存在")
            lessons = db.query_all(
                "SELECT id,title,objective,status FROM course_lessons WHERE unit_id = ?"
                " ORDER BY ordinal", (unit_id,)
            )
            lines = [
                f"【单元】{row['title']}（{row['summary'] or '无简介'}）", "",
                "【学过的讲次】",
            ]
            unfinished = [str(l["title"]) for l in lessons if l["status"] != "done"]
            if unfinished:
                lines += [
                    "", "【尚未学完】" + "、".join(unfinished)
                    + "（总结时请说明这些内容还没学过，不要假设已经掌握）",
                ]
            total_score = 0.0
            total_questions = 0
            for l in lessons:
                lines.append(f"- {l['title']}：{l['objective'] or '（目标未填写）'}")
                # 取最近一次作答统计
                last = db.query_one(
                    "SELECT MAX(attempt_no) AS n FROM practice_attempts WHERE lesson_id = ?",
                    (l["id"],),
                )
                if last and last["n"]:
                    stats = db.query_all(
                        "SELECT correct, score FROM practice_attempts"
                        " WHERE lesson_id = ? AND attempt_no = ?",
                        (l["id"], int(last["n"])),
                    )
                    if stats:
                        correct = sum(1 for s in stats if s["correct"])
                        score = sum(float(s["score"] or 0) for s in stats)
                        total_score += score
                        total_questions += len(stats)
                        lines.append(
                            f"  练习：{correct}/{len(stats)} 题正确，得分 {round(score, 2)}"
                        )
            lines += ["", "【答错/得分偏低的题】"]
            errors = db.query_all(
                "SELECT question, detail FROM course_errors WHERE lesson_id IN"
                " (SELECT id FROM course_lessons WHERE unit_id = ?) ORDER BY created_at",
                (unit_id,),
            )
            for e in errors[:20]:
                lines.append(f"- {e['question']}" + (f"（{e['detail'][:80]}）" if e["detail"] else ""))
            if not errors:
                lines.append("-（没有记录到错题）")

            self._set_stage(job_id, "正在生成总结")
            messages = [
                {"role": "system", "content": _UNIT_SUMMARY_PROMPT},
                {"role": "user", "content": "\n".join(lines)},
            ]
            obj: dict[str, Any] | None = None
            for _ in range(_MAX_RETRY + 1):
                raw = self._chat(messages, max_tokens=2048)
                parsed = self._safe_json(raw)
                if parsed is not None:
                    validated = self._validate_summary(parsed)
                    if validated is not None:
                        obj = validated
                        break
                obj = None
                messages += [
                    {"role": "assistant", "content": (raw or "")[:1200]},
                    {"role": "user", "content":
                        "上一次输出不符合 JSON 结构要求，请严格按 schema 重新输出，只输出一个 JSON 对象。"},
                ]
            if obj is None:
                obj = self._summary_fallback(lessons, errors)
                self._set_stage(job_id, "模型输出不稳定，已按学习记录生成总结")

            obj["stats"] = {
                "lessons": len(lessons),
                "questions": total_questions,
                "score": round(total_score, 2),
                "errors": len(errors),
            }
            md = self._summary_markdown(row["title"], obj)
            db.execute(
                "UPDATE course_units SET summary_json=?, summary_md=?, summary_status='ready',"
                " summary_error=NULL WHERE id=?",
                (json.dumps(obj, ensure_ascii=False), md, unit_id),
            )
            self._finish_job(job_id)
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.warning("单元总结生成失败", extra={"extra_fields": {"type": type(exc).__name__}})
            message = _err_text(exc)
            self._fail_job(job_id, message)
            get_db().execute(
                "UPDATE course_units SET summary_status='failed', summary_error=? WHERE id=?",
                (message[:500], unit_id),
            )

    @staticmethod
    def _validate_summary(obj: dict[str, Any]) -> dict[str, Any] | None:
        """校验单元总结结构。"""
        if not isinstance(obj, dict):
            return None
        recap = str(obj.get("recap") or "").strip()
        if not recap:
            return None

        def _list(key: str) -> list[str]:
            raw = obj.get(key)
            if not isinstance(raw, list):
                return []
            return [str(x).strip()[:200] for x in raw if str(x).strip()][:4]

        return {
            "recap": recap[:800],
            "mastered": _list("mastered"),
            "weak_points": _list("weak_points"),
            "next_steps": _list("next_steps"),
            "score_note": str(obj.get("score_note") or "").strip()[:300],
        }

    @staticmethod
    def _summary_fallback(lessons: Sequence[Any], errors: Sequence[Any]) -> dict[str, Any]:
        """确定性兜底总结：讲次即「学过的内容」，错题即「薄弱点」。"""
        return {
            "recap": "本单元共学完 " + str(len(lessons)) + " 节："
                     + "、".join(str(l["title"]) for l in list(lessons)[:5]) + "。",
            "mastered": ["完成本单元全部讲次"],
            "weak_points": [str(e["question"])[:120] for e in list(errors)[:4]]
                           or ["本次没有记录到错题"],
            "next_steps": ["针对上面的薄弱点回到对应讲次重看一遍", "做一次本单元的练习巩固"],
            "score_note": "（模型未返回合规总结，已按学习记录生成）",
        }

    @staticmethod
    def _summary_markdown(unit_title: str, obj: dict[str, Any]) -> str:
        """单元总结 → Markdown（导出用）。"""
        lines = [f"# 单元总结 · {unit_title}", "", obj.get("recap") or "", ""]
        for key, name in (("mastered", "已掌握"), ("weak_points", "待巩固"),
                          ("next_steps", "下一步")):
            items = obj.get(key) or []
            if not items:
                continue
            lines.append(f"## {name}")
            lines.append("")
            lines += [f"- {x}" for x in items]
            lines.append("")
        if obj.get("score_note"):
            lines += ["## 练习表现", "", obj["score_note"], ""]
        stats = obj.get("stats") or {}
        if stats:
            lines += [
                "> 统计：讲次 " + str(stats.get("lessons", 0))
                + " · 题目 " + str(stats.get("questions", 0))
                + " · 得分 " + str(stats.get("score", 0))
                + " · 错题 " + str(stats.get("errors", 0)),
            ]
        return "\n".join(lines)

    def get_unit_summary(self, unit_id: str) -> dict[str, Any]:
        """读取单元总结。"""
        row = get_db().query_one("SELECT * FROM course_units WHERE id = ?", (unit_id,))
        if row is None:
            raise AppError(1001, "单元不存在")
        return {
            "unit_id": unit_id,
            "title": row["title"],
            "status": _row_get(row, "summary_status") or "pending",
            "error": _row_get(row, "summary_error"),
            "summary": _json_loads(_row_get(row, "summary_json"), None),
            "markdown": _row_get(row, "summary_md"),
        }

    def complete_lesson(self, lesson_id: str) -> dict[str, Any]:
        """标记讲次完成（没有练习的讲次也能推进进度）。"""
        lesson = self._require_lesson(lesson_id)
        get_db().execute(
            "UPDATE course_lessons SET status='done', updated_at=? WHERE id=?",
            (now_iso(), lesson_id),
        )
        self._sync_unit_status(lesson["unit_id"])
        return {
            "lesson_id": lesson_id,
            "course_id": lesson["course_id"],
            "progress": self._progress(lesson["course_id"]),
        }

    def bind_conversation(self, lesson_id: str, conversation_id: str) -> dict[str, Any]:
        """把课堂对话绑定到讲次（下次进入同一讲次可接着问）。"""
        self._require_lesson(lesson_id)
        get_db().execute(
            "UPDATE course_lessons SET conversation_id=?, updated_at=? WHERE id=?",
            (str(conversation_id or "").strip() or None, now_iso(), lesson_id),
        )
        return {"lesson_id": lesson_id, "conversation_id": conversation_id}

    def save_courseware(
        self, lesson_id: str, slides: list[dict[str, Any]], scripts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """独立保存学生课件与讲师讲稿，保留二者的 ``slide_id`` 关联。"""
        row = self._require_lesson(lesson_id)
        clean_slides = self._clean_slides(slides)
        if not clean_slides:
            raise AppError(1000, "课件不能为空", "至少需要一页课件")
        clean_scripts = self._clean_scripts(
            scripts, {s["id"] for s in clean_slides}, clean_slides
        )
        board = _json_loads(row["board_json"], {}) or {}
        if not isinstance(board, dict):
            board = {}
        board["slides"] = clean_slides
        board["scripts"] = clean_scripts
        board["markdown"] = self._board_markdown(board)
        get_db().execute(
            "UPDATE course_lessons SET slides_json=?, script_json=?, board_json=?, board_md=?, updated_at=? WHERE id=?",
            (
                json.dumps(clean_slides, ensure_ascii=False),
                json.dumps(clean_scripts, ensure_ascii=False),
                json.dumps(board, ensure_ascii=False),
                board.get("markdown") or "",
                now_iso(),
                lesson_id,
            ),
        )
        return self.get_lesson(lesson_id)

    # ══════════════════════════════════════════════════
    # 讲次讲义（白板）
    # ══════════════════════════════════════════════════
    def generate_lecture(self, lesson_id: str) -> dict[str, Any]:
        """为讲次生成白板讲义（异步）。

        Returns:
            ``{lesson_id, job_id, status}``。
        """
        llm = LLMClient.get_instance()
        llm.ensure_configured()
        lesson = self._require_lesson(lesson_id)
        job_id = self._new_job(lesson["course_id"], lesson_id, "lecture", "正在准备讲义")
        t = threading.Thread(
            target=self._run_lecture, args=(lesson_id, job_id), daemon=True
        )
        t.start()
        return {"lesson_id": lesson_id, "job_id": job_id, "status": "running"}

    def _run_lecture(self, lesson_id: str, job_id: str) -> None:
        """后台线程：按讲次检索 → 生成白板 → 回填引用 → 落库。"""
        try:
            lesson = self._require_lesson(lesson_id)
            document_ids = self._course_document_ids(lesson["course_id"])
            query = " ".join(x for x in (lesson["title"], lesson["objective"]) if x)
            hits, context, table = self._material(document_ids, query, top_k=_MAX_HITS)
            if not hits:
                raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")

            self._set_stage(job_id, "正在生成讲义内容")
            unit_title = self._unit_title(lesson["unit_id"])
            prompt = (
                _LECTURE_PROMPT
                .replace("__DEPTH__", _DEPTH_HINT.get(lesson["depth"], _DEPTH_HINT["standard"]))
                .replace("__TITLE__", lesson["title"])
                .replace("__OBJECTIVE__", lesson["objective"] or lesson["title"])
                .replace("__UNIT__", unit_title)
            )
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": self._material_user(query, context)},
            ]

            obj: dict[str, Any] | None = None
            for _ in range(_MAX_RETRY + 1):
                raw = self._chat(messages, max_tokens=4096)
                parsed = self._safe_json(raw)
                if parsed is not None:
                    validated = self._validate_lecture(parsed)
                    if validated is not None:
                        obj = validated
                        break
                obj = None
                messages += [
                    {"role": "assistant", "content": (raw or "")[:1500]},
                    {"role": "user", "content":
                        "上一次输出不符合 JSON 结构要求，请严格按 schema 重新输出，只输出一个 JSON 对象。"},
                ]
            if obj is None:
                obj = self._lecture_fallback(lesson, hits)
                self._set_stage(job_id, "模型输出不稳定，已按材料片段生成讲义")

            board, citations = self._resolve_board(obj, table)
            db = get_db()
            db.execute(
                "UPDATE course_lessons SET board_json=?, board_md=?, slides_json=?, script_json=?, citations=?,"
                " status=CASE WHEN status='pending' THEN 'lecture_ready' ELSE status END,"
                " error=NULL, updated_at=? WHERE id=?",
                (
                    json.dumps(board, ensure_ascii=False),
                    board.get("markdown") or "",
                    json.dumps(board.get("slides") or [], ensure_ascii=False),
                    json.dumps(board.get("scripts") or [], ensure_ascii=False),
                    json.dumps(citations, ensure_ascii=False),
                    now_iso(),
                    lesson_id,
                ),
            )
            self._finish_job(job_id)
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.warning("讲义生成失败", extra={"extra_fields": {"type": type(exc).__name__}})
            message = _err_text(exc)
            self._fail_job(job_id, message)
            get_db().execute(
                "UPDATE course_lessons SET error=?, updated_at=? WHERE id=?",
                (message[:500], now_iso(), lesson_id),
            )

    @staticmethod
    def _clean_cards(raw: Any) -> list[dict[str, Any]]:
        """清洗讲义卡片（供模型输出、兼容迁移与编辑接口共用）。"""
        cards: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return cards
        for c in raw[:12]:
            if not isinstance(c, dict):
                continue
            title = str(c.get("title") or "").strip()
            body = str(c.get("body") or "").strip()
            if not title and not body:
                continue
            kind = str(c.get("kind") or "note").strip().lower()
            if kind not in ("concept", "example", "formula", "quote", "note"):
                kind = "note"
            cards.append({"kind": kind, "title": title[:120] or "要点", "body": body})
        return cards

    @staticmethod
    def _clean_slides(raw: Any) -> list[dict[str, Any]]:
        """清洗学生课件页：短内容、可展示、带稳定 id。"""
        slides: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return slides
        seen: set[str] = set()
        for i, item in enumerate(raw[:20], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            body = str(item.get("body") or "").strip()
            bullets_raw = item.get("bullets") or []
            bullets = [str(x).strip()[:180] for x in bullets_raw if str(x).strip()] if isinstance(bullets_raw, list) else []
            if not title and not body and not bullets:
                continue
            sid = str(item.get("id") or f"slide-{i}").strip() or f"slide-{i}"
            sid = re.sub(r"[^a-zA-Z0-9_-]+", "-", sid)[:50] or f"slide-{i}"
            while sid in seen:
                sid = f"slide-{i}-{len(seen) + 1}"
            seen.add(sid)
            kind = str(item.get("kind") or "note").strip().lower()
            if kind not in ("concept", "example", "formula", "quote", "note"):
                kind = "note"
            refs: list[int] = []
            for n in item.get("citation_refs") or []:
                try:
                    iv = int(n)
                except (TypeError, ValueError):
                    continue
                if iv > 0 and iv not in refs:
                    refs.append(iv)
            slides.append({
                "id": sid,
                "kind": kind,
                "title": title[:120] or "课件页",
                "bullets": bullets[:8],
                "body": body[:900],
                "citation_refs": refs[:8],
            })
        return slides

    @staticmethod
    def _clean_scripts(raw: Any, slide_ids: set[str], slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """清洗讲师讲稿，并按课件顺序补齐缺失页。"""
        by_id: dict[str, dict[str, Any]] = {}
        if isinstance(raw, list):
            for item in raw[:24]:
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("slide_id") or "").strip()
                text = str(item.get("text") or "").strip()
                if sid not in slide_ids or not text or sid in by_id:
                    continue
                by_id[sid] = {
                    "slide_id": sid,
                    "text": text[:4000],
                    "cue": str(item.get("cue") or "").strip()[:120],
                }
        scripts: list[dict[str, Any]] = []
        for slide in slides:
            sid = slide["id"]
            if sid in by_id:
                scripts.append(by_id[sid])
                continue
            scripts.append({
                "slide_id": sid,
                "text": CourseService._script_from_slide(slide),
                "cue": "legacy-fallback",
            })
        return scripts

    @staticmethod
    def _slides_from_cards(summary: str, cards: list[dict[str, Any]], recap: str) -> list[dict[str, Any]]:
        """旧讲义 cards → 兼容课件页。"""
        slides: list[dict[str, Any]] = []
        if summary:
            slides.append({
                "id": "slide-1", "kind": "concept", "title": "本讲导览",
                "bullets": [summary[:180]], "body": "", "citation_refs": [],
            })
        start = len(slides) + 1
        for i, c in enumerate(cards, start=start):
            body = str(c.get("body") or "")
            bullets = [x.strip() for x in re.split(r"[。；;\n]+", body) if x.strip()][:3]
            slides.append({
                "id": f"slide-{i}", "kind": c.get("kind") or "note",
                "title": str(c.get("title") or "要点")[:120],
                "bullets": bullets, "body": "", "citation_refs": [],
            })
        if recap:
            slides.append({
                "id": f"slide-{len(slides) + 1}", "kind": "note", "title": "本讲回顾",
                "bullets": [recap[:180]], "body": "", "citation_refs": [],
            })
        return slides

    @staticmethod
    def _script_from_slide(slide: dict[str, Any]) -> str:
        parts = [slide.get("title") or ""]
        parts.extend(slide.get("bullets") or [])
        if slide.get("body"):
            parts.append(slide["body"])
        return "。".join([str(x).strip() for x in parts if str(x).strip()])

    @staticmethod
    def _validate_lecture(obj: dict[str, Any]) -> dict[str, Any] | None:
        """校验课堂内容包：讲义 cards、学生 slides、讲师 scripts 分离。"""
        if not isinstance(obj, dict):
            return None
        summary = str(obj.get("summary") or "").strip()[:400]
        recap = str(obj.get("recap") or "").strip()[:600]
        cards = CourseService._clean_cards(obj.get("cards"))
        slides = CourseService._clean_slides(obj.get("slides"))
        if not slides and cards:
            slides = CourseService._slides_from_cards(summary, cards, recap)
        if not cards and slides:
            cards = [
                {
                    "kind": s.get("kind") or "note",
                    "title": s.get("title") or "要点",
                    "body": "\n".join(s.get("bullets") or []) or s.get("body") or "",
                }
                for s in slides
            ]
        if len(cards) < 3 or len(slides) < 3:
            return None
        slide_ids = {s["id"] for s in slides}
        scripts = CourseService._clean_scripts(obj.get("scripts"), slide_ids, slides)
        if len(scripts) != len(slides):
            return None

        keypoints: list[dict[str, str]] = []
        for k in (obj.get("keypoints") or [])[:12]:
            if not isinstance(k, dict):
                continue
            term = str(k.get("term") or "").strip()
            desc = str(k.get("desc") or "").strip()
            if term or desc:
                keypoints.append({"term": term[:80], "desc": desc})
        if len(keypoints) < 2:
            return None

        outline = [str(x).strip()[:150] for x in (obj.get("outline") or []) if str(x).strip()]

        # 材料标注意图（P1）：n 必须命中引用表，未命中的在 _resolve_board 里被丢弃。
        marks: list[dict[str, Any]] = []
        for mk in (obj.get("marks") or [])[:12]:
            if not isinstance(mk, dict):
                continue
            try:
                n = int(mk.get("n"))
            except (TypeError, ValueError):
                continue
            if n <= 0:
                continue
            kind = str(mk.get("kind") or "highlight").strip().lower()
            if kind not in ("highlight", "circle"):
                kind = "highlight"
            marks.append({
                "n": n, "kind": kind,
                "text": str(mk.get("text") or "").strip()[:200],
                "source": "model",
            })

        return {
            "summary": summary,
            "slides": slides,
            "scripts": scripts,
            "cards": cards,
            "outline": outline[:8],
            "keypoints": keypoints,
            "recap": recap,
            "marks": marks,
        }

    def _lecture_fallback(
        self, lesson: dict[str, Any], hits: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """确定性兜底讲义：课件、讲稿、讲义三套结构同时生成。"""
        cards: list[dict[str, Any]] = []
        for i, h in enumerate(hits[:6], start=1):
            snippet = str(h.get("snippet") or "").strip()
            if not snippet:
                continue
            cards.append({
                "kind": "quote" if i % 3 == 1 else "note",
                "title": f"材料片段 {i}"
                         + (f"· 第 {h['page_no']} 页" if h.get("page_no") else ""),
                "body": snippet[:400],
            })
        if not cards:
            cards = [{
                "kind": "note", "title": lesson["title"],
                "body": "材料中没有检索到可引用的片段，请补充资料后重新生成。",
            }]
        titles = [h.get("section") or h.get("document_title") or "" for h in hits[:5]]
        summary = f"本讲围绕「{lesson['title']}」展开。"
        recap = "（模型未返回合规讲义，已按材料片段生成）"
        slides = self._slides_from_cards(summary, cards, recap)
        scripts = [
            {
                "slide_id": s["id"],
                "text": f"这一页我们看「{s['title']}」。" + self._script_from_slide(s),
                "cue": "fallback",
            }
            for s in slides
        ]
        return {
            "summary": summary,
            "slides": slides,
            "scripts": scripts,
            "cards": cards,
            "outline": [t for t in dict.fromkeys([x for x in titles if x])][:5]
                       or [lesson["title"]],
            "keypoints": [{"term": lesson["title"], "desc": lesson["objective"] or "见材料片段"}],
            "recap": recap,
            "marks": [],
        }

    def _resolve_board(
        self, obj: dict[str, Any], table: dict[int, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """把课堂内容包里的 ``[[c:N]]`` 换成展示角标，并收集引用。"""
        merged: dict[int, dict[str, Any]] = {}

        def resolve(text: str) -> str:
            out, cites, _ = resolve_citations(text or "", table)
            for c in cites:
                merged.setdefault(c["n"], c)
            return out

        def touch_ref(n: Any) -> int | None:
            try:
                iv = int(n)
            except (TypeError, ValueError):
                return None
            if iv <= 0:
                return None
            _, cites, _ = resolve_citations(f"[[c:{iv}]]", table)
            for c in cites:
                merged.setdefault(c["n"], c)
            return iv if iv in table else None

        board = dict(obj)
        board["summary"] = resolve(obj.get("summary") or "")
        board["recap"] = resolve(obj.get("recap") or "")
        board["outline"] = [resolve(x) for x in (obj.get("outline") or [])]
        board["keypoints"] = [
            {
                "term": resolve(k.get("term") or ""),
                "desc": resolve(k.get("desc") or ""),
            }
            for k in (obj.get("keypoints") or [])
        ]
        board["slides"] = [
            {
                "id": s.get("id") or f"slide-{i}",
                "kind": s.get("kind") or "note",
                "title": resolve(s.get("title") or ""),
                "bullets": [resolve(x) for x in (s.get("bullets") or [])],
                "body": resolve(s.get("body") or ""),
                "citation_refs": [x for x in (touch_ref(n) for n in (s.get("citation_refs") or [])) if x],
            }
            for i, s in enumerate((obj.get("slides") or []), start=1)
        ]
        board["scripts"] = [
            {
                "slide_id": s.get("slide_id") or "",
                "text": resolve(s.get("text") or ""),
                "cue": resolve(s.get("cue") or ""),
            }
            for s in (obj.get("scripts") or [])
        ]
        board["cards"] = [
            {
                "kind": c.get("kind") or "note",
                "title": resolve(c.get("title") or ""),
                "body": resolve(c.get("body") or ""),
            }
            for c in (obj.get("cards") or [])
        ]

        # 标注：把模型给的 [[c:N]] 意图解析成具体材料位置。
        # **页码/文档一律来自引用表**（即 chunks 表），模型写不得。
        annotations: list[dict[str, Any]] = []
        for mk in (obj.get("marks") or []):
            hit = table.get(int(mk.get("n") or 0))
            if hit is None:
                continue  # 越界/未命中：丢弃，不生成无来源的标注
            if hit.document_id is None or hit.page_no is None:
                continue  # 无法定位到具体页的，不做标注
            text, _, _ = resolve_citations(str(mk.get("text") or ""), table)
            annotations.append({
                "n": int(mk["n"]),
                "document_id": hit.document_id,
                "document_title": hit.document_title,
                "page_no": hit.page_no,
                "kind": mk.get("kind") or "highlight",
                "text": text,
                "source": "model",
            })
        board["marks"] = annotations

        board["markdown"] = self._board_markdown(board)
        citations = [merged[k] for k in sorted(merged)]
        return board, citations

    @staticmethod
    def _board_markdown(board: dict[str, Any]) -> str:
        """课堂内容包 → Markdown（导出/回放/兜底展示用）。"""
        lines: list[str] = []
        if board.get("summary"):
            lines += [board["summary"], ""]
        if board.get("cards"):
            lines += ["## 讲义", ""]
        for c in board.get("cards") or []:
            lines.append(f"### {c.get('title') or '要点'}")
            lines.append("")
            if c.get("body"):
                lines += [c["body"], ""]
        if board.get("slides"):
            lines += ["## 课件", ""]
            for i, s in enumerate(board.get("slides") or [], start=1):
                lines += [f"### 第 {i} 页 · {s.get('title') or '课件页'}", ""]
                for b in s.get("bullets") or []:
                    lines.append(f"- {b}")
                if s.get("body"):
                    lines += ["", s["body"]]
                lines.append("")
        if board.get("scripts"):
            lines += ["## 讲师讲稿", ""]
            titles = {s.get("id"): s.get("title") for s in board.get("slides") or []}
            for i, sc in enumerate(board.get("scripts") or [], start=1):
                lines += [f"### 第 {i} 页 · {titles.get(sc.get('slide_id')) or sc.get('slide_id') or '讲稿'}", ""]
                if sc.get("text"):
                    lines += [sc["text"], ""]
        if board.get("keypoints"):
            lines += ["## 关键术语", "", "| 术语 | 说明 |", "| --- | --- |"]
            for k in board["keypoints"]:
                lines.append(f"| {k.get('term', '')} | {k.get('desc', '')} |")
            lines.append("")
        if board.get("recap"):
            lines += ["## 本讲回顾", "", board["recap"], ""]
        return "\n".join(lines).strip()

    # ══════════════════════════════════════════════════
    # 随堂练习
    # ══════════════════════════════════════════════════
    def generate_practice(self, lesson_id: str, count: int = 5) -> dict[str, Any]:
        """为讲次生成随堂练习（异步）。"""
        llm = LLMClient.get_instance()
        llm.ensure_configured()
        lesson = self._require_lesson(lesson_id)
        count = max(1, min(int(count or 5), 20))
        job_id = self._new_job(lesson["course_id"], lesson_id, "practice", "正在出题")
        t = threading.Thread(
            target=self._run_practice, args=(lesson_id, job_id, count), daemon=True
        )
        t.start()
        return {"lesson_id": lesson_id, "job_id": job_id, "status": "running"}

    def _run_practice(self, lesson_id: str, job_id: str, count: int) -> None:
        """后台线程：出题 → 校验 → 落库（保留已生成题目，便于重试续用）。"""
        try:
            lesson = self._require_lesson(lesson_id)
            document_ids = self._course_document_ids(lesson["course_id"])
            query = " ".join(x for x in (lesson["title"], lesson["objective"]) if x)
            hits, context, table = self._material(document_ids, query, top_k=_MAX_HITS)
            if not hits:
                raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")

            self._set_stage(job_id, "正在生成题目")
            prompt = (
                _PRACTICE_PROMPT
                .replace("__COUNT__", str(count))
                .replace("__DEPTH__", _DEPTH_HINT.get(lesson["depth"], _DEPTH_HINT["standard"]))
                .replace("__TITLE__", lesson["title"])
                .replace("__OBJECTIVE__", lesson["objective"] or lesson["title"])
            )
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": self._material_user(query, context)},
            ]

            items: list[dict[str, Any]] | None = None
            for _ in range(_MAX_RETRY + 1):
                raw = self._chat(messages, max_tokens=4096)
                parsed = self._safe_json(raw)
                if parsed is not None:
                    validated = self._validate_practice(parsed, count)
                    if validated:
                        items = validated
                        break
                items = None
                messages += [
                    {"role": "assistant", "content": (raw or "")[:1500]},
                    {"role": "user", "content":
                        "上一次输出不符合 JSON 结构要求，请严格按 schema 重新输出，只输出一个 JSON 对象。"},
                ]
            if not items:
                items = self._practice_fallback(lesson, hits, count)
                self._set_stage(job_id, "模型输出不稳定，已按材料片段生成题目")

            self._persist_practice(lesson_id, items, table)
            get_db().execute(
                "UPDATE course_lessons SET status=CASE WHEN status='done' THEN status"
                " ELSE 'practicing' END, error=NULL, updated_at=? WHERE id=?",
                (now_iso(), lesson_id),
            )
            self._finish_job(job_id)
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.warning("练习生成失败", extra={"extra_fields": {"type": type(exc).__name__}})
            message = _err_text(exc)
            self._fail_job(job_id, message)
            get_db().execute(
                "UPDATE course_lessons SET error=?, updated_at=? WHERE id=?",
                (message[:500], now_iso(), lesson_id),
            )

    @staticmethod
    def _validate_practice(obj: dict[str, Any], count: int) -> list[dict[str, Any]]:
        """校验并归一化题目列表；不合规的题直接丢弃。"""
        raw_items = obj.get("items") if isinstance(obj, dict) else obj
        if not isinstance(raw_items, list):
            return []
        out: list[dict[str, Any]] = []
        for it in raw_items[:count]:
            if not isinstance(it, dict):
                continue
            qtype = str(it.get("type") or "single").strip().lower()
            stem = str(it.get("stem") or it.get("question") or "").strip()
            if not stem:
                continue
            explanation = str(it.get("explanation") or "").strip()

            # 图片题（P1）：image 只接受 {"n":材料编号}，落库前换算成
            # {document_id,page_no} —— 编号由引用表解析，模型写不得具体页码。
            image: dict[str, Any] | None = None
            img_raw = it.get("image")
            if isinstance(img_raw, dict):
                try:
                    image = {"n": int(img_raw.get("n") or 0)}
                except (TypeError, ValueError):
                    image = None
            elif isinstance(img_raw, (int, float)) and img_raw:
                image = {"n": int(img_raw)}

            if qtype == "single":
                options = [str(o).strip() for o in (it.get("options") or []) if str(o).strip()]
                if len(options) < 2:
                    continue
                answer = _as_index(it.get("answer"), len(options))
                item: dict[str, Any] = {"type": "single", "stem": stem, "options": options,
                                        "answer": answer, "explanation": explanation}
                if image:
                    item["image"] = image
                out.append(item)

            elif qtype == "boolean":
                options = ["正确", "错误"]
                answer = _as_index(it.get("answer"), 2)
                item = {"type": "boolean", "stem": stem, "options": options,
                        "answer": answer, "explanation": explanation}
                if image:
                    item["image"] = image
                out.append(item)

            elif qtype == "fill_in":
                answer_raw = it.get("answer")
                if isinstance(answer_raw, str):
                    answers = [answer_raw]
                elif isinstance(answer_raw, list):
                    answers = [str(a) for a in answer_raw]
                else:
                    answers = [str(answer_raw or "")]
                answers = [a.strip() for a in answers if str(a).strip()]
                if not answers:
                    continue
                item = {"type": "fill_in", "stem": stem, "answer": answers,
                        "explanation": explanation}
                if image:
                    item["image"] = image
                out.append(item)

            elif qtype == "open":
                answer = str(it.get("answer") or "").strip()
                out.append({"type": "open", "stem": stem, "answer": answer,
                            "explanation": explanation})
        return out

    def _practice_fallback(
        self, lesson: dict[str, Any], hits: list[dict[str, Any]], count: int
    ) -> list[dict[str, Any]]:
        """确定性兜底题目：用材料片段改写成判断题与填空题。"""
        items: list[dict[str, Any]] = []
        for h in hits[: max(1, count)]:
            snippet = str(h.get("snippet") or "").strip().replace("\n", " ")
            if not snippet:
                continue
            head = snippet[:60]
            items.append({
                "type": "boolean",
                "stem": f"判断：材料中提到「{head}…」",
                "answer": 0,
                "explanation": "出自《" + str(h.get("document_title") or "材料") + "》"
                               + (f" 第 {h['page_no']} 页" if h.get("page_no") else ""),
            })
            if len(items) >= count:
                break
        if not items:
            items = [{
                "type": "open",
                "stem": f"用自己的话复述「{lesson['title']}」的核心内容。",
                "answer": lesson["objective"] or "见讲义",
                "explanation": "模型未返回合规题目，已改为开放题。",
            }]
        return items

    def _persist_practice(
        self,
        lesson_id: str,
        items: list[dict[str, Any]],
        table: dict[int, Any],
    ) -> None:
        """题目落库（作答一并清空，避免与新题错位）。"""
        db = get_db()
        ts = now_iso()
        with db.transaction():
            db.execute("DELETE FROM practice_questions WHERE lesson_id = ?", (lesson_id,))
            for i, it in enumerate(items, start=1):
                # 注意：resolve_citations 返回 (展示文本, 引用列表, 丢弃数)，
                # 这里要的是**引用列表**，别把第三个返回值当成引用。
                stem, cites, _ = resolve_citations(it["stem"], table)
                explanation, cites2, _ = resolve_citations(it.get("explanation") or "", table)
                source = (cites or cites2 or [None])[0]

                # 图片题：把材料编号换算成真实 {document_id, page_no}。
                image_json = None
                img_n = (it.get("image") or {}).get("n") if isinstance(it.get("image"), dict) else None
                hit = table.get(int(img_n)) if img_n else None
                if hit is not None and hit.page_no is not None:
                    image_json = json.dumps(
                        {"document_id": hit.document_id, "page_no": hit.page_no,
                         "document_title": hit.document_title},
                        ensure_ascii=False,
                    )

                if it["type"] == "fill_in":
                    answer = json.dumps([_strip_marks(a, table) for a in it["answer"]],
                                        ensure_ascii=False)
                elif it["type"] in ("single", "boolean"):
                    answer = json.dumps(int(it["answer"]), ensure_ascii=False)
                else:
                    answer = json.dumps(_strip_marks(str(it["answer"]), table),
                                        ensure_ascii=False)
                db.execute(
                    "INSERT INTO practice_questions(id,lesson_id,ordinal,type,stem,options,"
                    "answer,explanation,image,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id(), lesson_id, i, it["type"], stem,
                        json.dumps(it.get("options") or [], ensure_ascii=False)
                        if it.get("options") else None,
                        answer, explanation, image_json,
                        json.dumps(source, ensure_ascii=False) if source else None,
                        ts,
                    ),
                )

    @staticmethod
    def list_questions(lesson_id: str) -> list[dict[str, Any]]:
        """列出讲次题目（不含正确答案，前端据此渲染）。"""
        db = get_db()
        rows = db.query_all(
            "SELECT * FROM practice_questions WHERE lesson_id = ? ORDER BY ordinal",
            (lesson_id,),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            item: dict[str, Any] = {
                "id": r["id"], "ordinal": r["ordinal"], "type": r["type"],
                "stem": r["stem"], "has_source": bool(r["source"]),
            }
            if r["options"]:
                item["options"] = _json_loads(r["options"], [])
            # 图片题：返回材料定位信息，前端用内置 pdf.js 渲染该页
            # （服务端渲染需 PyMuPDF/AGPL，默认不启用，见 coursemedia）。
            image = _json_loads(_row_get(r, "image"), None)
            if isinstance(image, dict) and image.get("document_id") and image.get("page_no"):
                item["image"] = {
                    "document_id": image["document_id"],
                    "page_no": int(image["page_no"]),
                    "document_title": image.get("document_title") or "",
                }
                item["image_url"] = (
                    f"/api/courses/documents/{image['document_id']}/raw"
                )
            out.append(item)
        return out

    # ── 判分 ────────────────────────────────────────────
    def grade(
        self, lesson_id: str, answers: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        """逐题判分并写入作答记录。

        客观题（单选/判断/填空）**本地确定性判分**，不经过模型；
        开放题调用模型按评分标准打分，失败时给 0.6 并标注需人工复核。

        Returns:
            ``{total, correct_count, score, percent, results:[...]}``。
        """
        lesson = self._require_lesson(lesson_id)
        db = get_db()
        rows = db.query_all(
            "SELECT * FROM practice_questions WHERE lesson_id = ? ORDER BY ordinal",
            (lesson_id,),
        )
        if not rows:
            raise AppError(1002, "本讲还没有题目", "请先生成随堂练习")

        answer_map: dict[str, Any] = {}
        for a in answers or []:
            if isinstance(a, dict) and a.get("question_id"):
                answer_map[str(a["question_id"])] = a.get("answer")

        # 同一讲次可重做：作答次数递增，单元总结取**最近一次**的成绩。
        last = db.query_one(
            "SELECT MAX(attempt_no) AS n FROM practice_attempts WHERE lesson_id = ?",
            (lesson_id,),
        )
        attempt_no = int(last["n"] or 0) + 1

        results: list[dict[str, Any]] = []
        for r in rows:
            raw_answer = answer_map.get(r["id"])
            res = self._grade_one(r, raw_answer)
            db.execute(
                "INSERT INTO practice_attempts(id,lesson_id,question_id,attempt_no,answer,"
                "correct,score,feedback,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    new_id(), lesson_id, r["id"], attempt_no,
                    json.dumps(raw_answer, ensure_ascii=False),
                    1 if res["correct"] else 0, float(res["score"]),
                    res["feedback"][:1000], now_iso(),
                ),
            )
            results.append({
                "question_id": r["id"],
                "ordinal": r["ordinal"],
                "type": r["type"],
                "stem": r["stem"],
                "correct": res["correct"],
                "score": res["score"],
                "feedback": res["feedback"],
                "expected": res["expected"],
            })

        total = len(results)
        correct_count = sum(1 for x in results if x["correct"])
        score = round(sum(float(x["score"]) for x in results), 2)
        percent = round(score * 100 / total, 1) if total else 0.0

        # 完成练习即标记讲次完成，并推进单元/课程状态。
        db.execute(
            "UPDATE course_lessons SET status='done', updated_at=? WHERE id=?",
            (now_iso(), lesson_id),
        )
        self._sync_unit_status(lesson["unit_id"])

        # 错题写入课程错题本（后续复习用）。
        for x in results:
            if not x["correct"]:
                db.execute(
                    "INSERT INTO course_errors(id,course_id,lesson_id,question,detail,"
                    "created_at) VALUES (?,?,?,?,?,?)",
                    (new_id(), lesson["course_id"], lesson_id, x["stem"][:300],
                     x["feedback"][:500], now_iso()),
                )

        return {
            "lesson_id": lesson_id,
            "attempt_no": attempt_no,
            "total": total,
            "correct_count": correct_count,
            "score": score,
            "percent": percent,
            "results": results,
        }

    def _grade_one(self, row: Any, raw_answer: Any) -> dict[str, Any]:
        """单题判分（客观题确定性；开放题走模型）。"""
        qtype = row["type"]
        if qtype in ("single", "boolean"):
            options = _json_loads(row["options"], [])
            expected = _json_loads(row["answer"], 0)
            try:
                expected_i = int(expected)
            except (TypeError, ValueError):
                expected_i = 0
            answer_i = _as_index(raw_answer, max(1, len(options)))
            correct = answer_i == expected_i
            label = options[expected_i] if 0 <= expected_i < len(options) else str(expected_i)
            return {
                "correct": correct,
                "score": 1.0 if correct else 0.0,
                "feedback": row["explanation"] or ("回答正确" if correct else "再想想"),
                "expected": label,
            }

        if qtype == "fill_in":
            answers = [a for a in _json_loads(row["answer"], []) if str(a).strip()]
            given = _norm_text(str(raw_answer or ""))
            correct = bool(given) and any(_norm_text(a) == given for a in answers)
            return {
                "correct": correct,
                "score": 1.0 if correct else 0.0,
                "feedback": row["explanation"]
                            or (f"参考答案：{' / '.join(answers[:3])}" if answers else ""),
                "expected": " / ".join(answers[:3]),
            }

        # 开放题：模型评分，失败时给中性分并标注。
        reference = _json_loads(row["answer"], "")
        if isinstance(reference, (list, dict)):
            reference = json.dumps(reference, ensure_ascii=False)
        given = str(raw_answer or "").strip()
        if not given:
            return {
                "correct": False, "score": 0.0,
                "feedback": "未作答。" + (f"参考答案：{reference}" if reference else ""),
                "expected": str(reference)[:200],
            }
        try:
            prompt = (
                _GRADE_PROMPT
                .replace("__STEM__", row["stem"])
                .replace("__ANSWER__", str(reference)[:800])
                .replace("__RESPONSE__", given[:1500])
            )
            obj = LLMClient.get_instance().chat_json(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "请评分。"},
                ],
                temperature=0.2,
                max_tokens=800,
            )
            try:
                score = float(obj.get("score", 0))
            except (TypeError, ValueError):
                score = 0.0
            score = max(0.0, min(1.0, score))
            correct = bool(obj.get("correct", score >= 0.6))
            feedback = str(obj.get("feedback") or "").strip()
        except Exception as exc:  # noqa: BLE001 - 判分失败不能卡住练习
            logger.warning("开放题判分失败", extra={"extra_fields": {"type": type(exc).__name__}})
            score, correct, feedback = 0.6, True, "自动判分未成功，已按参考作答计分，建议自行复核。"
        return {
            "correct": correct, "score": round(score, 2),
            "feedback": feedback or (row["explanation"] or ""),
            "expected": str(reference)[:200],
        }

    @staticmethod
    def _sync_unit_status(unit_id: str) -> None:
        """单元内所有讲次完成 → 单元完成，并激活下一单元。"""
        db = get_db()
        row = db.query_one("SELECT course_id, ordinal FROM course_units WHERE id = ?", (unit_id,))
        if row is None:
            return
        pending = db.query_one(
            "SELECT COUNT(*) AS n FROM course_lessons WHERE unit_id = ? AND status <> 'done'",
            (unit_id,),
        )
        if int(pending["n"] or 0) == 0:
            db.execute("UPDATE course_units SET status='done' WHERE id = ?", (unit_id,))
            nxt = db.query_one(
                "SELECT id FROM course_units WHERE course_id = ? AND ordinal > ?"
                " ORDER BY ordinal LIMIT 1",
                (row["course_id"], row["ordinal"]),
            )
            if nxt is not None:
                db.execute(
                    "UPDATE course_units SET status='active' WHERE id = ?", (nxt["id"],)
                )

    # ══════════════════════════════════════════════════
    # 任务（前端轮询）
    # ══════════════════════════════════════════════════
    def get_job(self, job_id: str) -> dict[str, Any]:
        """读取生成任务状态。"""
        row = get_db().query_one("SELECT * FROM course_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise AppError(1001, "任务不存在")
        return self._job_out(row)

    def list_jobs(self, course_id: str) -> list[dict[str, Any]]:
        """列出课程的生成任务（最新在前）。"""
        rows = get_db().query_all(
            "SELECT * FROM course_jobs WHERE course_id = ? ORDER BY created_at DESC LIMIT 20",
            (course_id,),
        )
        return [self._job_out(r) for r in rows]

    @staticmethod
    def _new_job(course_id: str, lesson_id: str | None, kind: str, stage: str) -> str:
        """创建任务记录，返回 ``job_id``。"""
        jid = new_id()
        ts = now_iso()
        get_db().execute(
            "INSERT INTO course_jobs(id,course_id,lesson_id,kind,stage,status,error,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (jid, course_id, lesson_id, kind, stage, "running", None, ts, ts),
        )
        return jid

    @staticmethod
    def _set_stage(job_id: str, stage: str) -> None:
        """更新任务阶段文案（前端展示生成进度）。"""
        get_db().execute(
            "UPDATE course_jobs SET stage=?, updated_at=? WHERE id=?",
            (stage, now_iso(), job_id),
        )

    @staticmethod
    def _finish_job(job_id: str) -> None:
        """标记任务完成。"""
        get_db().execute(
            "UPDATE course_jobs SET status='ready', stage='已完成', updated_at=? WHERE id=?",
            (now_iso(), job_id),
        )

    @staticmethod
    def _fail_job(job_id: str, error: str) -> None:
        """标记任务失败。"""
        get_db().execute(
            "UPDATE course_jobs SET status='failed', error=?, updated_at=? WHERE id=?",
            (error[:500], now_iso(), job_id),
        )

    @staticmethod
    def _fail_course(cid: str, error: str) -> None:
        """课程生成失败（保留课程记录，便于重试）。"""
        get_db().execute(
            "UPDATE courses SET status='failed', error=?, updated_at=? WHERE id=?",
            (error[:500], now_iso(), cid),
        )

    # ══════════════════════════════════════════════════
    # 内部工具
    # ══════════════════════════════════════════════════
    @staticmethod
    def _require_lesson(lesson_id: str) -> dict[str, Any]:
        """读取讲次，不存在抛 1001。"""
        row = get_db().query_one("SELECT * FROM course_lessons WHERE id = ?", (lesson_id,))
        if row is None:
            raise AppError(1001, "讲次不存在")
        return dict(row)

    @staticmethod
    def _course_document_ids(cid: str) -> list[str]:
        """课程绑定的材料 id。"""
        rows = get_db().query_all(
            "SELECT document_id FROM course_documents WHERE course_id = ?", (cid,)
        )
        return [r["document_id"] for r in rows]

    @staticmethod
    def _ready_document_ids(limit: int = 10) -> list[str]:
        """已解析完成的材料 id。"""
        rows = get_db().query_all(
            "SELECT id FROM documents WHERE status='ready' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [r["id"] for r in rows]

    @staticmethod
    def _unit_title(unit_id: str) -> str:
        """单元标题。"""
        row = get_db().query_one("SELECT title FROM course_units WHERE id = ?", (unit_id,))
        return row["title"] if row else ""

    @staticmethod
    def _draft_title(goal: str, document_ids: list[str]) -> str:
        """创建时的临时标题（取目标首句，等模型给出正式标题）。"""
        first = re.split(r"[。！？\n]", (goal or "").strip())[0]
        first = first.strip()[:40]
        if first:
            return first
        if document_ids:
            row = get_db().query_one(
                "SELECT title FROM documents WHERE id = ?", (document_ids[0],)
            )
            if row:
                return f"《{row['title']}》课程"
        return "新的课程"

    @classmethod
    def _material(
        cls,
        document_ids: Sequence[str],
        query: str,
        top_k: int = _MAX_HITS,
    ) -> tuple[list[dict[str, Any]], str, dict[int, Any]]:
        """按查询检索课程材料，返回 ``(hits, 注入用上下文, 引用表)``。

        ``引用表`` 是本轮页码的**唯一**权威来源（见 :mod:`citations`）。

        课程场景的查询常常是**讲次标题/目标短语**（如「极限是什么」），与正文
        的词汇交集很小，纯检索容易 0 命中 → 讲义空着。这里在检索为空时兜一层
        **材料概览**（取课程绑定材料的开头切片），它同样带真实 ``chunk_id``，
        引用防线不受影响（与聊天侧的「文档级意图」兜底同一思路）。
        """
        ids = list(document_ids)
        hits, _, _ = get_retrieval_service().hybrid_search(
            query or "课程重点", document_ids=ids or None, top_k=top_k
        )
        if not hits and ids:
            hits, outline = get_retrieval_service().material_overview(ids)
            if hits:
                context, table = build_context(hits, outline=outline)
                return hits, context, table
        context, table = build_context(hits)
        return hits, context, table

    @staticmethod
    def _material_user(query: str, context: str) -> str:
        """构造注入模型的用户消息（学习任务 + 编号材料）。"""
        head = f"【学习任务】{query}\n\n" if query else ""
        return f"{head}【学习材料检索结果】\n{context}"

    @staticmethod
    def _chat(messages: list[dict[str, str]], *, max_tokens: int = 4096) -> str:
        """调用对话模型（统一温度）。"""
        return LLMClient.get_instance().chat(
            messages, temperature=0.4, max_tokens=max_tokens
        )

    @staticmethod
    def _safe_json(raw: str) -> dict[str, Any] | None:
        """解析模型输出，失败返回 ``None``（不抛异常，交给上层降级）。"""
        try:
            return extract_json_object(raw)
        except Exception:  # noqa: BLE001 - 解析失败即视为不可用例
            return None

    # ── 输出序列化 ──────────────────────────────────────
    def _course_out(
        self,
        row: Any,
        *,
        with_units: bool = False,
        with_progress: bool = False,
    ) -> dict[str, Any]:
        """课程行 → 对外字典。"""
        out: dict[str, Any] = {
            "id": row["id"],
            "title": row["title"],
            "goal": row["goal"],
            "level": row["level"],
            "level_name": _LEVEL_NAME.get(row["level"], row["level"]),
            "depth": row["depth"],
            "depth_name": _DEPTH_NAME.get(row["depth"], row["depth"]),
            "unit_count": row["unit_count"],
            "summary": row["summary"],
            "status": row["status"],
            "error": row["error"],
            "document_ids": self._course_document_ids(row["id"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if with_progress:
            out["progress"] = self._progress(row["id"])
        if with_units:
            db = get_db()
            units: list[dict[str, Any]] = []
            for u in db.query_all(
                "SELECT * FROM course_units WHERE course_id = ? ORDER BY ordinal",
                (row["id"],),
            ):
                lessons = [
                    self._lesson_out(l)
                    for l in db.query_all(
                        "SELECT * FROM course_lessons WHERE unit_id = ? ORDER BY ordinal",
                        (u["id"],),
                    )
                ]
                units.append(self._unit_out(u, lessons=lessons))
            out["units"] = units
        return out

    def _unit_out(self, row: Any, *, lessons: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """单元行 → 对外字典（含总结与练习成绩统计）。

        注意：``summary`` 字段历史上是**单元简介**（用户在结构确认时可改），
        这里保持向后兼容；生成出来的单元总结放在 ``summary_data``。
        """
        out = {
            "id": row["id"], "ordinal": row["ordinal"], "title": row["title"],
            "summary": row["summary"], "status": row["status"],
            "summary_status": _row_get(row, "summary_status") or "pending",
            "summary_error": _row_get(row, "summary_error"),
            "summary_data": _json_loads(_row_get(row, "summary_json"), None),
            "summary_md": _row_get(row, "summary_md"),
            "lessons": lessons if lessons is not None else [],
        }
        if lessons:
            out["stats"] = self._unit_stats(row["id"], lessons)
        return out

    @staticmethod
    def _unit_stats(unit_id: str, lessons: list[dict[str, Any]]) -> dict[str, Any]:
        """单元练习统计（按每节**最近一次**作答汇总）。"""
        db = get_db()
        total = correct = 0
        score = 0.0
        for l in lessons:
            last = db.query_one(
                "SELECT MAX(attempt_no) AS n FROM practice_attempts WHERE lesson_id = ?",
                (l["id"],),
            )
            if not last or not last["n"]:
                continue
            rows = db.query_all(
                "SELECT correct, score FROM practice_attempts"
                " WHERE lesson_id = ? AND attempt_no = ?",
                (l["id"], int(last["n"])),
            )
            total += len(rows)
            correct += sum(1 for r in rows if r["correct"])
            score += sum(float(r["score"] or 0) for r in rows)
        errors = db.query_one(
            "SELECT COUNT(*) AS n FROM course_errors WHERE lesson_id IN"
            " (SELECT id FROM course_lessons WHERE unit_id = ?)",
            (unit_id,),
        )
        return {
            "lessons": len(lessons),
            "done_lessons": sum(1 for l in lessons if l["status"] == "done"),
            "questions": total,
            "correct": correct,
            "score": round(score, 2),
            "errors": int((errors or {"n": 0})["n"] or 0),
        }

    def _lesson_out(self, row: Any) -> dict[str, Any]:
        """讲次行 → 对外字典（含白板、引用与材料标注）。"""
        db = get_db()
        marks = _json_loads(_row_get(row, "board_marks"), [])
        questions = db.query_one(
            "SELECT COUNT(*) AS n FROM practice_questions WHERE lesson_id = ?", (row["id"],)
        )
        board = _json_loads(row["board_json"], None)
        if isinstance(board, dict):
            slides = _json_loads(_row_get(row, "slides_json"), None)
            scripts = _json_loads(_row_get(row, "script_json"), None)
            if not isinstance(slides, list) or not slides:
                slides = board.get("slides") if isinstance(board.get("slides"), list) else None
            if not isinstance(slides, list) or not slides:
                slides = self._slides_from_cards(
                    str(board.get("summary") or ""),
                    self._clean_cards(board.get("cards")),
                    str(board.get("recap") or ""),
                )
            if not isinstance(scripts, list) or not scripts:
                scripts = board.get("scripts") if isinstance(board.get("scripts"), list) else None
            if not isinstance(scripts, list) or not scripts:
                scripts = self._clean_scripts([], {s["id"] for s in slides}, slides)
            board.setdefault("slides", slides)
            board.setdefault("scripts", scripts)
        else:
            slides = []
            scripts = []
        return {
            "id": row["id"],
            "course_id": row["course_id"],
            "unit_id": row["unit_id"],
            "ordinal": row["ordinal"],
            "global_ordinal": row["global_ordinal"],
            "kind": row["kind"],
            "kind_name": _KIND_NAME.get(row["kind"], row["kind"]),
            "title": row["title"],
            "objective": row["objective"],
            "depth": row["depth"],
            "status": row["status"],
            "board": board,
            "slides": slides,
            "scripts": scripts,
            "board_md": row["board_md"],
            "marks": marks if isinstance(marks, list) else [],
            "question_count": int(questions["n"] or 0),
            "citations": _json_loads(row["citations"], []),
            "conversation_id": row["conversation_id"],
            "model": row["model"],
            "error": row["error"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _job_out(row: Any) -> dict[str, Any]:
        """任务行 → 对外字典。"""
        return {
            "id": row["id"], "course_id": row["course_id"], "lesson_id": row["lesson_id"],
            "kind": row["kind"], "stage": row["stage"], "status": row["status"],
            "error": row["error"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def _as_index(value: Any, size: int) -> int:
    """把模型的答案（下标 / ``"A"`` / ``"正确"``）统一转成 0 起下标。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        i = int(value)
        return i if 0 <= i < max(1, size) else 0
    text = str(value or "").strip()
    if text.isdigit():
        i = int(text)
        return i if 0 <= i < max(1, size) else 0
    upper = text.upper()
    if len(upper) == 1 and "A" <= upper <= "Z":
        i = ord(upper) - 65
        return i if 0 <= i < max(1, size) else 0
    if text in ("正确", "对", "是", "True", "true"):
        return 0
    if text in ("错误", "错", "否", "False", "false"):
        return 1
    return 0


def _strip_marks(text: str, table: dict[int, Any]) -> str:
    """去掉文本里的 ``[[c:N]]`` 标记（答案不需要角标）。"""
    out, _, _ = resolve_citations(text or "", table)
    return out


def get_course_service() -> CourseService:
    """返回课程服务单例。"""
    return CourseService.get_instance()
