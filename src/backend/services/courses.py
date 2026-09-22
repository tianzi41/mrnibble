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

import difflib
import json
import logging
import math
import re
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..db.connection import get_db
from ..errors import AppError
from ..utils.ids import new_id
from ..utils.timeutil import now_iso, parse_iso
from . import diagram as diagram_mod
from .citations import build_context, resolve_citations
from .llm import LLMClient, extract_json_object
from .profile import with_profile
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

# 从「重新生成」补充说明里解析用户写死的单元数量（见 regenerate_outline）。
# 中文数字：零~九 + 十/两；必须紧跟单位词才算命中，clamp 1~12，取最后一个匹配。
# 位置词（最后/前/后/第/这/那/每/上/下/某个）前缀的不算数量——「最后一个单元」
# 是序数而非「1 个单元」，会被负向前查排除。
_CN_UNIT_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_UNIT_WORDS = r"(?:章|单元|部分|模块|块)"
_UNIT_QUALIFIER = r"(?<![最后前第这那每上下某个])"
_UNIT_RE_ARABIC = re.compile(_UNIT_QUALIFIER + r"(\d+)\s*(?:个)?\s*" + _UNIT_WORDS)
_UNIT_RE_CN = re.compile(_UNIT_QUALIFIER + r"([零一二两三四五六七八九十])\s*(?:个)?\s*" + _UNIT_WORDS)


def _unit_count_from_note(note: str) -> int | None:
    """从用户的补充要求里解析写死的单元数（如「一共生成五章」）。

    - 阿拉伯数字与中文数字都识别（零~九、十、两）；
    - **必须紧跟单位词**（章/单元/部分/模块/块，允许中间夹「个」）才算命中，
      因此「五」（无单位词）、「第十单元」（序数，带「第」）都不计入数量；
    - clamp 到 1~12（与单元数域一致），**取最后一个匹配**
      （用户可能写「不要 3 章，要 5 章」）；
    - 解析不出返回 ``None``。
    """
    if not note:
        return None
    found: list[int] = []
    for m in _UNIT_RE_ARABIC.finditer(note):
        found.append(int(m.group(1)))
    for m in _UNIT_RE_CN.finditer(note):
        found.append(_CN_UNIT_DIGITS[m.group(1)])
    if not found:
        return None
    return max(1, min(12, found[-1]))
# 生成任务防重入：running 任务超过该时长（分钟）视为僵死（进程被杀等遗留），
# 不再计入防重入 —— 否则用户会被永远挡住，只能靠重启进程解困。
_JOB_REENTRY_STALE_MIN = 15

# 用户把学习目标一键清空时，后端按课型兜底出来的「目的句」（见 ``create_course``）。
# 它不含任何材料语义，**不能**当检索查询词用 —— 否则会召回与目标无关的片段、
# 污染大纲的单元切法。识别出来即只按材料概览组织大纲。
_FALLBACK_GOAL_RE = re.compile(r"^按「[^」]+」的方式学这门课")

# ``courses.hands_on`` 的三态取值：1 = 包含实操、0 = 纯理论、2 = **自动**（按材料判断）。
# 为什么不用 NULL 表示「自动」：该列是 `INTEGER NOT NULL DEFAULT 1`（schema v5 / migrations v5），
# 插 NULL 会直接 IntegrityError 把建课接口打崩（2026-09-17 实测踩到）。
# 用 2 的好处：**不需要迁移**；旧程序读到 2 时 `bool(2)` = True（含实操），行为安全。
_HANDS_ON, _HANDS_OFF, _HANDS_AUTO = 1, 0, 2


def _goal_hits(goal: str, ids: list[str]) -> list[dict[str, Any]]:
    """按学习目标检索补充片段（0 命中返回空表）。

    目标是「按课型兜底」的句子时**直接返回空**：那种句子不含材料语义，
    召回的多是与课程无关的片段，会把单元切法带偏；结构交给材料概览决定即可
    （与 :meth:`CourseService._material` 的空结果兜底同一思路）。
    """
    if _FALLBACK_GOAL_RE.match((goal or "").strip()):
        return []
    hits, _, _ = get_retrieval_service().hybrid_search(
        goal, document_ids=ids or None, top_k=6
    )
    return list(hits)


# ── 「讲稿不许照念课件」的结构判定阈值 ──────────────────
# 归一化后 difflib 相似度达到该值 → 判定为逐字复述课件。
# 为什么要做结构判定而不是只写进提示词：模型（尤其小模型）会反复
# 退化成「把课件念一遍」，只在提示词里叮嘱无法保证；这里用确定性的
# 文本比较拦下，再重写一次，仍不合格走确定性扩写兜底。
_SCRIPT_DUP_RATIO = 0.82
# 相似度偏高时，讲稿还需显著长于课件文本才算「展开了」。
_SCRIPT_DUP_SOFT_RATIO = 0.70
_SCRIPT_MIN_EXPAND = 1.25

# ── 通用规则（所有课程生成提示词共用）───────────────────
_BASE_RULES = """通用规则：
1. **只依据下方 [材料N]** 组织内容；引用一律写 [[c:编号]]（编号取自 [材料N] 实际给出的 N，不得使用未出现的编号）；禁止自己书写页码、章节号或文件名。
2. 材料里没有的内容不要编造；确需补充时以「（材料外补充）」开头，每次生成最多 2 处。
3. 全部内容使用简体中文（专有名词、公式符号可保留原文）。
4. 只输出一个 JSON 对象，不要输出 JSON 以外的任何文字（包括解释与代码块标记），不要输出 schema 之外的字段。"""

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

# ── 讲次教学设计 desc 的字段规范（**单一来源**）─────────────
# 「生成大纲」与「为旧课程补写 desc」两处提示词都引用它。曾经因为一段编辑竞态，
# 大纲提示词里的整段 desc 规范被静默吃掉，只留下自查清单里两句提及——模型
# 完全不知道要输出 desc 字段（而测试用的是带 desc 的 mock 桩，因此没暴露）。
# 教训：提示词里的关键字段规范必须**可被断言**（diagram_ir_check 有同源断言），
# 且只写一份。
_DESC_SPEC = """每讲必须同时产出 desc（本讲教学设计）——它是后续**逐讲独立生成讲义**时的唯一约束：
逐讲调用看不到其它讲次，讲间边界与术语口径全靠 desc 划清。
- 正文讲（kind=lecture）的 desc 六个字段：
  outcomes：学习目标 1~3 条（每条 ≤40 字），写「学完能做到什么」，与 objective 呼应但不重复措辞；
  knowledge_points：知识点边界 2~5 条（每条 ≤30 字），**只列本讲要讲的内容**——相邻讲次会讲的**一律不列**；
  concepts：本讲必须解释清楚的核心术语 ≤4 个（每个 ≤12 字），**全课程统一译名**，同一概念在所有讲次用同一写法；
  operations：涉及的具体操作/命令/演示 ≤4 条（每条 ≤16 字），没有就省略；
  transition：{"prev":"承接上一讲的什么（≤25 字）","next":"给下一讲引什么（≤25 字）",
    "avoid":"本讲**不展开**、留给相邻讲次的内容（≤25 字，防讲间重复的关键字段）"}；
  visual：可视化提示（≤40 字，具体到「什么图/什么表」，如「流程：字节→代码页→乱码」；没有可视觉化内容写「无」）。
- 练习讲（kind=practice）的 desc 用**另一组**字段（不要照抄正文讲字段）：
  exercise_focus：考察点 ≤3 条（每条 ≤30 字），指明考的是哪几讲的内容；
  expected_mistakes：学生易错点 ≤3 条（每条 ≤30 字），供出题时设计干扰项；
  exercise_flow：题型安排（≤60 字，如「4 道判断 → 2 道改错 → 1 道综合归类」）。"""

# 为**已有旧课程**补写 desc（这些课建于 desc 功能上线前，desc_json 为 NULL）。
# 只补 desc，绝不动结构/标题/讲次/已生成的讲义。
_DESC_FILL_PROMPT = """你是课程设计师。下面是一门**已经定稿**的课程结构（单元 + 每讲标题与学习目标）。
请为**每一讲**补写「本讲教学设计」desc，供后续逐讲生成讲义时约束内容。

**严禁改动结构**：不得增删单元或讲次、不得改标题、不得改变顺序与 kind。
只输出 desc，其余字段一律原样不动。

__DESC_SPEC__

额外要求：
- 严格按现有讲次的标题与 objective 推断该讲该讲什么，不要替它换主题；
- 讲间边界要**落在实际结构上**：transition.prev 指向结构里排在它前面的那一讲，
  transition.next 指向排在它后面的那一讲（用标题指代即可）；
- 练习讲的 exercise_focus 必须指向同一单元里**前面那些正文讲**的内容；
- **title 必须原样回带输入的讲次标题，逐字一致**（不增删改字），便于服务端校验顺序。

输出 JSON（单元与讲次顺序必须与输入完全一致，数量也必须一致）：
{"units":[{"lessons":[
  {"title":"原样回填输入里的讲次标题","desc":{"outcomes":["..."],"knowledge_points":["..."],"concepts":["..."],"operations":["..."],
           "transition":{"prev":"...","next":"...","avoid":"..."},"visual":"..."}},
  {"title":"原样回填输入里的讲次标题","desc":{"exercise_focus":["..."],"expected_mistakes":["..."],"exercise_flow":"..."}}]}]}

输出前自查（只自查，不输出过程）：
- 讲次数量与顺序是否与输入完全一致？
- 正文讲是否给了 outcomes/knowledge_points/concepts 三组必备字段？
- 练习讲是否用了 exercise_focus/expected_mistakes/exercise_flow（而不是正文讲字段）？
- 同一个术语在所有讲次的 concepts 里是否用了同一个译名？
""" + _BASE_RULES

# ── 课型（学习意图）库 —— **单一来源** ─────────────────────
# 用户要控制的是「想要什么形态的课」，而不是让 AI 替他写一串能力目标。
# 课型同时是**结构模板**：决定单元怎么切、讲次按什么顺序走 ——
# 所以提示词里的推进顺序直接取这里的 flow（与 desc 规范同样的「一份定义」原则）。
#
# live=False 是**数据结构预留**：上线只需改成 True，前端经接口自动出现，
# 不需要改前端代码、也不需要数据迁移。
_INTENT_TYPES: tuple[dict[str, Any], ...] = (
    # ── 已上线：通用 5 种 ──────────────────────────────
    {"id": "overview", "live": True, "group": "general", "name": "了解脉络型",
     "fit": "只求大概了解：背景、人物、事件脉络、主旨",
     "pace": "轻量通识，1 课时以内",
     "flow": ["导入与目标", "作者/出处/时代背景", "通读正音", "事件脉络图或结构图",
              "人物性格卡", "主旨与启示", "小结与简单练习"],
     "assist": "脉络图/结构图与人物关系梳理"},
    {"id": "deep-read", "live": True, "group": "general", "name": "由浅入深精读型",
     "fit": "先了解大概，再逐字逐句学习（常规教学主流）",
     "pace": "最像学校常规课件，2~3 课时",
     "flow": ["整体概览", "背景与人物", "初读正音断句",
              "逐段逐句精讲：原文 · 重点字词 · 翻译 · 特殊句式 · 内容作用",
              "段意与结构", "主旨与写法", "总结", "检测"],
     "assist": "逐字逐句的字词与翻译讲法"},
    {"id": "exam", "live": True, "group": "general", "name": "考点应试型",
     "fit": "要提分、要复习：重点字词、翻译、断句、内容理解",
     "pace": "目标明确，适合考前复习",
     "flow": ["考情分析", "重点实词虚词", "通假字 / 古今异义 / 词类活用",
              "特殊句式", "翻译训练", "断句技巧", "文意理解题", "真题演练", "错题总结"],
     "assist": "考点密度与练习量"},
    {"id": "inquiry", "live": True, "group": "general", "name": "主题探究型",
     "fit": "不想只翻译，要讨论、探究、展示",
     "pace": "适合公开课与素养课",
     "flow": ["抛出核心问题", "背景铺垫", "文本细读找证据", "分组探究",
              "辩论或汇报", "主旨升华", "写作迁移", "总结"],
     "assist": "问题链与讨论/探究环节"},
    {"id": "project", "live": True, "group": "general", "name": "任务驱动型",
     "fit": "要以成果收尾：讲稿、海报、讲解视频、演示程序",
     "pace": "参与度高，适合拓展课与跨学科",
     "flow": ["真实情境任务", "拆解子任务", "材料阅读与资料搜集", "成果制作",
              "展示与评价", "总结反思"],
     "assist": "实操与成果产出环节"},
    # ── 预留（live=False）：把 live 改成 True 即上线 ──────
    {"id": "recite", "live": True, "group": "subject-zh", "name": "诵读涵泳型",
     "fit": "诗词、美文、经典：重语感、节奏、情感、背诵",
     "pace": "适合诗词与经典散文",
     "flow": ["初读正音", "节奏停顿", "范读/跟读", "字词疏通", "画面与意象",
              "情感变化", "手法赏析", "背诵默写", "总结"],
     "assist": "节奏、范读与背诵环节"},
    {"id": "character", "live": True, "group": "subject-zh", "name": "人物形象探究型",
     "fit": "史传、传记、记人叙事：重点分析人物性格与评价",
     "pace": "适合史传类文本",
     "flow": ["人物档案", "生平时间轴", "典型事件", "细节描写", "性格多面性",
              "作者评价", "历史影响", "启示与迁移", "总结"],
     "assist": "人物细节与性格分析"},
    {"id": "culture", "live": True, "group": "subject-zh", "name": "文化专题型",
     "fit": "想讲礼制、官职、地理、典故、民俗",
     "pace": "适合文化信息密集的文本",
     "flow": ["文化导入", "关键文化点", "文本印证", "背景拓展", "古今对比",
              "文化意义", "总结"],
     "assist": "文化常识与古今对照"},
    {"id": "contrast", "live": True, "group": "general", "name": "对比阅读型",
     "fit": "两篇或多篇材料一起讲：求同、比异",
     "pace": "适合单元复习与群文阅读",
     "flow": ["选文组合", "求同", "比异", "人物/主旨/手法比较", "背景比较",
              "迁移写作", "总结"],
     "assist": "对照表与差异分析"},
    {"id": "micro", "live": True, "group": "general", "name": "微课型",
     "fit": "时间短、课前自学或课后复习",
     "pace": "每页信息少、节奏快，5~15 分钟",
     "flow": ["课前任务单", "微课讲重点", "当堂检测", "课后拓展"],
     "assist": "压缩篇幅、突出单点"},
)


def intent_catalog() -> list[dict[str, Any]]:
    """已上线的课型清单（前端经接口取，避免前后端各写一份而漂移）。"""
    return [dict(t) for t in _INTENT_TYPES if t["live"]]


def intent_by_id(iid: str | None) -> dict[str, Any] | None:
    """按 id 取课型（只认已上线的；给 ``None``/未知 id 返回 ``None``）。"""
    if not iid:
        return None
    for t in _INTENT_TYPES:
        if t["id"] == iid and t["live"]:
            return t
    return None


def _intent_out(raw: Any) -> dict[str, Any] | None:
    """课型 → 对外输出（**带上名称**，前端就不必再查一次课型库）。"""
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001 - 脏数据按无课型处理
        return None
    if not isinstance(obj, dict) or not obj.get("primary"):
        return None
    p = intent_by_id(str(obj.get("primary")))
    if p is None:
        return None
    a = intent_by_id(str(obj.get("assist") or ""))
    return {
        "primary": p["id"],
        "primary_name": p["name"],
        "assist": a["id"] if a else None,
        "assist_name": a["name"] if a else None,
        "note": str(obj.get("note") or "") or None,
    }


def _intent_spec(primary_id: str | None, assist_id: str | None, note: str = "") -> str:
    """课型 → 提示词块（大纲阶段的结构约束）。无有效主课型时返回空串（旧课程不受影响）。"""
    p = intent_by_id(primary_id)
    if p is None:
        return ""
    lines = [
        "【本次课型（用户明确选定：决定单元如何划分、讲次按什么顺序走）】",
        f"主课型：{p['name']} —— {p['fit']}",
        "按它推进：" + " → ".join(str(x) for x in p["flow"]),
    ]
    a = intent_by_id(assist_id)
    if a is not None and a["id"] != p["id"]:
        lines.append(f"辅助课型：{a['name']}（在满足主课型结构的前提下，额外加强：{a['assist']}）")
    if note:
        lines.append("用户补充：" + note)
    lines.append(
        "要求：单元划分与讲次顺序要**贴着上面的推进顺序**（可按材料实际增删环节，"
        "但顺序与侧重不得反着来）；材料明显不支撑某个环节时跳过它、**不要硬造**；"
        "课型只决定「顺序与侧重」，具体知识点仍然只能来自材料。"
    )
    return "\n".join(lines)


# 课型推荐（创建向导「分析材料」）：按材料判断最合适的课型。
_INTENT_PICK_PROMPT = """你是课程顾问。用户选好了学习材料，请判断这些材料更适合哪种课型。

可选课型（**只能**从这里选 id）：
__INTENT_LIST__

判断依据是材料的体裁与内容：技术文档/教程偏「由浅入深精读型」或「任务驱动型」；
文言文原文与注释偏「由浅入深精读型」；只讲背景梗概的偏「了解脉络型」；
要备考做题的偏「考点应试型」；有可讨论话题的偏「主题探究型」。

输出 JSON：
{"primary":"主课型id","reason":"为什么适合（≤30 字，指出材料里像的地方）","alternatives":["备选id1","备选id2"]}
只输出这一个 JSON 对象，不要输出 JSON 以外的任何文字（包括解释与代码块标记）。
"""

# 按课型写「目的句」（取代原来的「4 条能力目标」）。
_GOAL_FROM_INTENT_PROMPT = """你是学习规划师。用户已经选定了材料与想要的课型，请为这门课写**一句**学习目标。

要求：
- 只写**一句话**（40~80 字），写成「我想要的目的是什么」，不是能力清单；
- 用目的口吻，例：「读懂《孔雀东南飞》全文，能顺畅翻译并说清故事与人物关系，顺带掌握常见实词虚词」；
- **禁止**罗列「能…能…能…」的多条目标 —— 那些是一门课该产出的成果，不是用户的目的；
- 要体现所选课型的侧重（课型信息见用户消息中【本次课型】）；
- 只依据材料真实覆盖的内容，不要编造材料里没有的主题。

输出 JSON：{"goal":"一句话"}
只输出这一个 JSON 对象，不要输出 JSON 以外的任何文字（包括解释与代码块标记）。
"""

_OUTLINE_PROMPT = """你是课程设计师。请依据用户的学习目标与材料，设计一门「能学完」的课程大纲。
学习者水平与深度要求见用户消息中的【课程信息】。

__INTENT_SPEC__

结构要求：
- 课程标题：一句话点明主题，具体到方法或领域，不空泛；
- __UNITS_RULE__；
- __LESSONS_RULE__；
- 每个单元最后一个讲次固定为 practice（随堂练习），objective 写「检验本单元各讲目标是否达成」；其余为 lecture；
  **练习也计入讲次数**（例如「2 讲」= 1 讲正文 + 1 讲练习）；
- 讲次顺序由浅入深；同一单元内讲次的 depth 大体递进；
- 去重：每个讲次聚焦一个学习目标，不同讲次的 objective 不得相近或重复。

字段写法：
- 讲次 title 用「主题 · 侧重点」形式，点明这一讲的角度（例：「洛必达法则 · 什么时候不能用」），不要与单元标题相同；
- 讲次 objective 以「能 + 可检验动词」开头（能说明/能计算/能推导/能区分/能应用/能判断），
  禁止「了解/熟悉/掌握/学习」等无法检验的动词。
  反例（禁止）：「了解极限的概念」。正例：「能用自己的话解释极限的直观含义，并判断给定数列是否收敛」；
- 讲次 depth 四选一（认知层级，决定该讲讲义的讲法）：
  establish——建立动机与直观理解（为什么需要它、它解决什么问题）；
  define——给出准确定义、符号与适用条件；
  derive——推导性质、证明结论或分析原理；
  apply——用它完成具体例题或应用；
- 单元 summary：1~2 句，说明该单元在整门课中的角色（承接什么、为后续铺垫什么）。

__DESC_SPEC__

覆盖要求：
- 优先按材料中体现的章节/主题组织单元；材料覆盖的主要内容不得留整块缺口；
- 材料里没有的主题不得编造；用户目标与材料冲突时以材料为准，并在 summary 里说明取舍。

输出 JSON：
{"title":"课程标题","summary":"课程简介（2~3 句，说明适合谁、学完能做什么）",
 "units":[{"title":"单元标题","summary":"单元简介",
 "lessons":[
   {"title":"讲次标题","objective":"学习目标","kind":"lecture","depth":"establish|define|derive|apply",
    "desc":{"outcomes":["学完能做到什么"],"knowledge_points":["本讲要讲的知识点"],
            "concepts":["核心术语"],"operations":["涉及的操作"],
            "transition":{"prev":"承接什么","next":"引向什么","avoid":"本讲不展开的内容"},
            "visual":"可视化提示，没有写「无」"}},
   {"title":"练习讲标题","objective":"检验本单元各讲目标是否达成","kind":"practice","depth":"apply",
    "desc":{"exercise_focus":["考察哪几讲的内容"],"expected_mistakes":["学生易错点"],
            "exercise_flow":"题型安排"}}]}]}

输出前自查（只自查，不输出过程）：
- 是否有任意两个讲次的 objective 说的其实是同一件事？
- objective 里有没有「了解/熟悉/掌握」？
- 材料的主要章节是否都有讲次覆盖？
- 每个单元的讲次数是否**由内容体量决定**？有没有把一讲就能讲完的内容硬拆成两讲凑数？
- 每讲的 desc 是否划清了与相邻讲次的边界（transition.avoid 写了吗）？
- 同一个术语在所有讲次的 desc.concepts 里是否用了同一个译名？
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

# 大纲 vs 材料对齐校验（B 防线）：desc 是后续所有讲义共同的上游，大纲阶段对材料的
# 误解会「一致放大」到每一讲且难以察觉——出纲后用一次调用把编造的知识点拉回材料。
_ALIGN_PROMPT = """你是课程审校。下面是一份为材料设计的课程大纲（含每讲教学设计 desc）。
请对照材料核对：每讲 desc 里是否混入了**材料并不支持的知识点**（大纲替材料想出来的内容）、
术语译名是否与材料一致。只修正 desc，不要改动标题/结构/讲次数量。

输出 JSON 二选一：
{"ok":true}
或 {"ok":false,"fixes":[{"unit":单元序号(1起),"lesson":讲序号(单元内1起),"desc":{该讲修正后的完整 desc}}]}
只输出这一个 JSON 对象，不要输出 JSON 以外的任何文字（包括解释与代码块标记）。
"""


def _norm_desc(raw: Any, kind: str) -> dict[str, Any] | None:
    """归一化讲次教学设计 desc（模型输出不可信：缺字段/类型不对/超长一律清洗）。

    正文讲与练习讲字段集不同，按 ``kind`` 切换；清洗后一条不剩则返回 ``None``。
    """
    if not isinstance(raw, dict):
        return None

    def scalar(v: Any) -> str:
        """只接受标量（str/int/float），dict/list/None 一律空串——防 repr 原文混进 desc。"""
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v).strip()
        return ""

    def strs(v: Any, limit: int, width: int) -> list[str]:
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for x in v[:limit]:
            s = scalar(x)
            if s:
                out.append(s[:width])
        return out

    if kind == "practice":
        d: dict[str, Any] = {
            "exercise_focus": strs(raw.get("exercise_focus"), 3, 30),
            "expected_mistakes": strs(raw.get("expected_mistakes"), 3, 30),
        }
        flow = scalar(raw.get("exercise_flow"))[:60]
        if flow:
            d["exercise_flow"] = flow
    else:
        d = {
            "outcomes": strs(raw.get("outcomes"), 3, 40),
            "knowledge_points": strs(raw.get("knowledge_points"), 5, 30),
            "concepts": strs(raw.get("concepts"), 4, 12),
            "operations": strs(raw.get("operations"), 4, 16),
        }
        tr = raw.get("transition")
        if isinstance(tr, dict):
            t = {k: scalar(tr.get(k))[:25] for k in ("prev", "next", "avoid")}
            t = {k: v for k, v in t.items() if v}
            if t:
                d["transition"] = t
        vis = scalar(raw.get("visual"))[:40]
        if vis and vis != "无":
            d["visual"] = vis
    return {k: v for k, v in d.items() if v} or None

_LECTURE_PROMPT = """你是课堂讲师，正在给一门真实课程备课。请为下面这一讲生成一版「课堂内容包」。

【讲次信息】
讲次：__TITLE__
学习目标（学完这一讲能做到什么）：__OBJECTIVE__
所属单元：__UNIT__
__DESC__
__SIBLINGS__

**跨讲边界（硬要求）**：
- 讲次地图里其他讲次的专属主题，在本讲**最多用一句话点到**（形如「这里先记住结论，第 3 讲会展开」），**不得**为它安排课件页或展开讲解；
- 「引向」字段提到的主题属于**后续讲次**，只允许出现在最后一页的一句话预告里，不得作为任何页的主要内容；
- 若某一页其实是在讲别的讲次的主题，把它换成「本讲边界内、本讲尚未覆盖」的内容；
- 本讲没有新内容可讲时，宁可把页数压到【深度与篇幅】的下限，**也不要重复或稀释**。

【深度与篇幅】
__DEPTH__

这一讲产出三类内容，读者与形态必须分开：
- slides（课件页）：学生课堂上跟着看的。每页 = 一个短标题 + 最多 5 条要点（每条 ≤25 字）或一个例子/公式；禁止把整段讲解塞进 bullets 或 body；
  bullets 里的**关键术语、编号、结论词用加粗标出**（Markdown 双星号，每页 ≤3 处），方便一眼抓重点；
- scripts（讲师讲稿）：讲课时朗读的。每页一段口语化的讲解——解释、举例、衔接；禁止复述课件；
- cards（讲义卡片）：学生课后通读的。每张一个主题，比课件页完整：要说清「为什么」，并指出常见误区。

课件页形态示例（好，内容须换成本讲材料）：
{"id":"slide-2","kind":"example","title":"0/0 型长什么样","bullets":["分子分母同时趋于 0","直接代入得到 0/0，算不出值","例：lim(x→0) sinx/x"],"body":"","citation_refs":[3]}
坏课件页（禁止）：body 里塞 200 字整段讲解——那是讲稿或讲义的事。

课件页类型（kind）使用时机：
- concept：核心概念或结论；example：例子/例题；formula：公式/表达式（附适用条件）；
- quote：材料原句直引（必须带 [[c:N]]）；note：注意事项、易错点、衔接说明；
- diagram：**硬要求** —— 本讲内容只要涉及「有先后顺序的步骤 / 谁调用谁 / 数据流向 /
  状态或阶段变化 / 组件分层」中的任意一种，就**必须**至少安排 1 页 diagram；
  只有确实一种都不涉及（纯名词解释、纯心态建议之类）才允许不出图。
  按内容选图型，结构写进 diagram 字段（见下方【可视化页与特色页】，**只写结构化 IR，不写图形语法**）；
- chart：材料里有能成图的数值对比时；数据写入 chart 字段。材料没有现成数字就不要硬造图表。
- table：**两个或多个方案/编码/观点的逐项对照**优先用表格（该对比就该用表格，别硬画成图）；
  内容写入 table 字段（见下方【可视化页与特色页】）；
- takeaway：本讲最想让人记住的一句结论或教练点评；内容写入 takeaway 字段。

【可视化页与特色页（diagram/chart/table 三类合计每讲最多 2 页，计入 slides 总页数；
  takeaway 是文字形态，不计入该上限，每讲最多 1 页。
  **下限要求（硬要求）**：本讲若涉及「流程 / 调用关系 / 数据流向 / 状态变化 / 组件分层」之一
  → 必须 ≥1 页 diagram；若出现「两/多种方案或做法的逐项对照」→ 必须 ≥1 页 table。
  两者都出现时合计仍不超过 2 页，此时优先 diagram）】
__DIAGRAM_SPEC__
- chart 页：kind="chart"，加字段
  "chart":{"type":"bar|line|pie","title":"图表标题","unit":"数值单位（可选）","categories":["类目1","类目2"],"series":[{"name":"系列名","data":[12,30]}]}。
  数据只能来自材料原文（或由材料数字直接换算），**禁止编造数值**；类目 ≤12 个；系列 ≤2 条；
  categories 数量必须与每条 series 的 data 数量一致；pie 只给 1 条 series。
- diagram/chart 页仍必须有 title 与 1~3 条 bullets（图旁要点，也是图渲染失败时学生看到的回退内容）；
  scripts 照常为该页写讲稿，而且**要真的在讲这张图**（不许只念图上的字）：
  先说清这张图整体在画什么，再带着听众走一遍关键路径，最后落到结论。按图型各有侧重——
  workflow 说「从哪开始、依次经过哪几步、哪一步最容易出错」；
  sequence 说「谁先请求谁、返回了什么、什么时候会走另一条路」；
  dataflow 说「数据从哪来、中间被做了什么变换、最后落到哪里」；
  lifecycle 说「什么事件让状态发生迁移、正常终态与异常终态分别是什么」；
  architecture 说「分成哪几层/哪几个边界、主要组件各自负责什么、主路径怎么走」。
- table 页：kind="table"，加字段
  "table":{"title":"表题（可选）","columns":["列1","列2"],"rows":[["单元1","说明1"],["单元2","说明2"]]}。
  只用于逐项对照（两方案/多编码/多观点的异同）；列数 2~5、数据行 ≤8、单元格 ≤40 字；
  单元格只写短词或短语，不要整句；内容只能来自材料原文，禁止编造；
- takeaway 页：kind="takeaway"，加字段 "takeaway":"一句 ≤60 字的结论或教练点评"。
  通常放在最后做收尾金句；同样必须有 title；整讲最多 1 页。
- 上面两条「必须」的情形都没出现时，才允许整讲没有可视化页；
  **不要为了凑数硬造图，也不要编造表格里的数据**。

输出 JSON：
{"summary":"本讲一句话导览",
 "slides":[{"id":"slide-1","kind":"concept|example|formula|quote|note|diagram|chart|table|takeaway","title":"课件页标题","bullets":["短要点1","短要点2"],"body":"可选补充短句，可含 [[c:N]]","citation_refs":[1]}],
 "scripts":[{"slide_id":"slide-1","text":"讲师实际朗读的完整讲稿，可含 [[c:N]]。要解释课件、补充上下文和自然转场。"}],
 "cards":[{"kind":"concept|example|formula|quote|note","title":"讲义卡片标题","body":"讲义正文，可含 [[c:N]]"}],
 "outline":["要点1","要点2"],
 "keypoints":[{"term":"术语/公式","desc":"解释"}],
 "recap":"本讲回顾（3 句以内）",
 "marks":[{"n":1,"kind":"highlight","text":"这句为什么重要"}]}
"diagram"/"chart"/"table"/"takeaway" 都是**可选字段**，仅对应 kind 的页才出现，格式见上方【可视化页与特色页】；
diagram 的写法是 "diagram":{"ir":{...}}（**只给 IR 对象，不要给 svg/html/坐标**）；

质量要求：
1. 叙事结构：第 1 页**点出本讲要解决的问题**为主，确需承接上一讲时只用一句话回扣、**不得重述上一讲的背景或课件内容**；最后一页做小结，中间由浅入深；
2. **讲稿 ≠ 课件文字（最重要）**。禁止把课件标题与要点原样念一遍。
   每段讲稿满足【深度与篇幅】里的字数要求之外，至少包含「解释为什么」「举一个例子」「和上一页衔接」中的两类。
   反例（禁止）：slide 写「洛必达法则适用于 0/0 型」，script 也写「洛必达法则适用于 0/0 型」。
   正例：script 写「我们刚才看到这个式子上下都趋于零，直接代入算不出来。这时候洛必达法则才有用——
   它要求分子分母同时趋于零（或同时趋于无穷），也就是所谓的 0/0 型。举个最常见的例子……」；
3. scripts 与 slides 逐页一一对应，slide_id 必须来自 slides，数量相同；
4. cards 至少 3 张、至少 1 张 quote（直引材料原句并标 [[c:N]]）；kind 为 concept 的卡片至少 1 处标注 [[c:N]]；
   keypoints 至少 2 条，term 必须是本讲 slides 或 scripts 里实际出现过的术语/公式；
5. 讲稿口语化，但不得加入材料与课件都没有的具体数字或结论；拿不准的量只转述材料原文并标 [[c:N]]；
6. 引用一律 [[c:N]]，N 只能是材料实际给出的编号；citation_refs 只写本页用到的编号。
7. **首讲不做全景导览**：若本讲是课程或单元的第 1 讲，不得逐一预告后续各讲主题、不得把后续讲次的核心内容提前讲完；最多一句话说明「本单元分几块、后面依次展开」。（可结合讲次地图判断自己是不是第 1 讲。）

marks 是**材料标注意图**：n 必须是本讲用过的 [[c:N]] 编号，kind 取 highlight（黄底高亮）或 circle（红圈），
text 是写在旁边的一句旁注。没有把握就返回空数组，不要编造 n。

输出前自查（只自查，不输出过程）：
- 每段讲稿读起来像老师说话吗？有没有哪段只是在念课件？
- slides 每页是否一眼能看完（短标题 + 短要点）？
- 引用编号是否都来自材料？
- 可视化页（diagram/chart/table）是否 ≤2 页？chart 里的数字、table 里的内容是否都来自材料？
- 本讲是否涉及流程/调用/数据流向/状态变化/分层？若涉及，有没有安排 diagram 页（**硬要求**）？
- 本讲出现两/多种做法的对照了吗？若出现，有没有用 table（**硬要求**）？
- 本讲页数有没有达到【深度与篇幅】写的下限？不够就补页，**不是**把每页写长。
- 对比类内容（两方案/多编码/多观点）有没有用 table 而不是硬写成要点？takeaway 是不是只有一页？
- diagram 页的图型选对了吗（有先后顺序用 workflow、谁调用谁用 sequence、数据流向用 dataflow、
  状态变化用 lifecycle、组件分层用 architecture）？
- IR 里的每个节点与关系是否都来自材料？有没有误加坐标、样式、SVG 或 Mermaid 语法之类的字段？
""" + _BASE_RULES

# 「讲稿照念课件」被结构判定拦下后的定向重写提示词（只重写有问题的页）。
_SCRIPT_REWRITE_PROMPT = """你是课堂讲师。下面列出的课件页，对应的**讲师讲稿**
只是把课件文字念了一遍——这是不合格的。

请为每一页重写讲稿，要求：
1. 绝对不要照抄课件的标题或要点原句，必须换成口语化的讲解；
2. 每段讲稿至少包含「解释为什么这样」「举一个具体例子」「和上下文衔接」中的两类；
3. 长度明显长于课件文字（建议 1.5 倍以上），读起来像老师在讲台上说话，不是念 PPT；
4. 如引用材料仍用 [[c:N]]，不要写页码。
5. 每段重写讲稿不少于 120 字（课件页太短时「1.5 倍」标准失效，用绝对下限兜底）；
6. 原讲稿中已有的 [[c:N]] 引用若仍成立，重写后保留。

待重写的页：
__PAYLOAD__

只输出 JSON：{"scripts":[{"slide_id":"原始 slide id","text":"重写后的讲稿"}]}
不要输出 JSON 以外的任何文字。
"""

# 跨讲去重：检测到本讲课件页标题与同课程其他已落库讲次重复时，定向重写命中的页。
# 结构照抄 _SCRIPT_REWRITE_PROMPT（只重写有问题的页、只输出有限 JSON）。
_DEDUP_REWRITE_PROMPT = """你是课堂讲师。这一讲在生成后，发现以下课件页的**标题**
与同一门课里其他讲次已经讲过的页面**完全重合**（重复课件，学员会看到一模一样的内容）。

请在不改变本讲主题与结构的前提下，把这些重复的页**改写到本讲边界内、其他讲次尚未覆盖**的内容：
- 换标题与要点，使其只讲本讲该讲的东西；
- 不得把其他讲次的主题拉进来凑数；
- 改写后字数、引用规则与正常课件页一致，引用仍用 [[c:N]]。

待改写的页：
__PAYLOAD__

只输出 JSON：{"slides":[{"id":"原始 slide id","kind":"concept|example|formula|quote|note|diagram|chart|table|takeaway","title":"改写后的课件页标题","bullets":["短要点1","短要点2"],"body":"可选补充短句，可含 [[c:N]]","citation_refs":[1]}]}
不要输出 JSON 以外的任何文字。
"""

_PRACTICE_PROMPT = """你是出题老师。请围绕这一讲出 __COUNT__ 道题，__DEPTH__。

讲次：__TITLE__
目标：__OBJECTIVE__
__DESC__

输出 JSON：
{"items":[{"type":"single","stem":"题干","options":["A","B","C","D"],"answer":0,"explanation":"解析"},
          {"type":"boolean","stem":"判断题干","options":["正确","错误"],"answer":1,"explanation":"解析"},
          {"type":"fill_in","stem":"填空题（用 ____ 表示待填）","answer":["标准答案","同义答案"],"explanation":"解析"},
          {"type":"hands_on","stem":"打开 CMD 输入 chcp 并回车，把你看到的活动代码页编号填进来","answer":["936","65001"],"explanation":"解析"},
          {"type":"open","stem":"开放题","answer":"参考答案","explanation":"评分要点"},
          {"type":"single","stem":"看图题：图中所示的结论是什么","options":["A","B","C","D"],"answer":0,
           "explanation":"解析","image":{"n":1}}]}

要求：
- **题型配比以单选为主**：5 题时 3~4 道 single，最多 1 道 boolean、最多 1 道 fill_in、最多 1 道 open；
  题量更少时优先保证 single；开放题只在确实需要展开论述时使用；
- 单选题 answer 是正确选项的**下标**（0 起）；判断题 options 固定 ["正确","错误"]，answer 为 0 或 1；
- fill_in 的 answer 是**可接受答案数组**（可含同义写法）；
- open 的 answer 是参考答案文本；
- **回填式实操题（hands_on）**：题干写清「动手做什么、把什么结果填进来」（真的运行一条命令、
  真的打开某个设置项看一眼），answer 是可接受的回填值数组（写法同 fill_in，含常见等价写法）。
  最多 1 道，且**只在讲次内容确实涉及可真实操作的主题时**才出；纯理论内容不要硬造；
- **图片题**：需要看图时加 "image":{"n":材料编号}，n 必须是你引用过的材料编号，
  系统会把材料对应页渲染成图；最多出 1 道图片题，没有合适的图就不要加 image 字段；
- 每题都要有 explanation；全部题目必须来自本讲内容。
- **选项之间必须有实质区别**：同一道题里不得出现两个只差空格、标点、大小写、前导斜杠或
  反引号包裹的选项（例如 ``/C:/Users/PC`` 与 ``C:/Users/PC`` 视为同一个，属废题）；
- **不得与本课程其他讲次的题目重复**：哪怕考同一个知识点，也要换角度提问（对比、反例、应用场景）。
""" + _BASE_RULES

_DIAGRAM_REWRITE_PROMPT = """你是课堂图示编辑。下面这张课件图的 IR 没有通过确定性校验。

【校验诊断】（规则码 + 出错位置 + 证据）
__DIAGNOSTICS__

【建议修复】
__FIXES__

【原始 IR】
__IR__

请修正这张图，要求：
1. **只修被诊断点名的对象**，其余节点、关系与字段保持原样；不要重命名 id，除非诊断明确要求；
2. 严格遵守字段契约：只能出现规定的字段，**不要给坐标、样式、SVG、Mermaid 语法**；
3. 数量与字数上限继续遵守（节点 ≤12、关系 ≤18、标签 ≤14 字）；
4. 修不好的部分可以整条删掉（少一个节点，也比一张错图好）。

只输出 JSON：{"diagram":{"ir":<修正后的 IR 对象>}}
不要输出 JSON 以外的任何文字。
"""

_VISUAL_ADD_PROMPT = """你是课件补图编辑。这一讲的课件页**没有任何可视化内容**，
但材料里确实出现了「流程 / 调用 / 数据流向 / 状态变化 / 分层 / 对照」这类结构——
所以这一讲应该有图或表，只是上一轮漏了。

讲次：__TITLE__
目标：__OBJECTIVE__

【现有课件页】
__SLIDES__

【材料片段】
__CONTEXT__

请**新增 1 页**可视化页（插在小结之前），二选一，哪个更贴合材料就用哪个：
- kind="diagram"：材料里有先后步骤 / 谁调用谁 / 数据流向 / 状态变化 / 组件分层。
  优先画材料里最核心的那条流程或状态变化；
- kind="table"：材料里有两/多种做法的逐项对照（参数、编码、方案、误区对照）。

要求：
1. 该页必须有 title 与 1~3 条 bullets（图旁要点，也是渲染失败时学生看到的回退内容）；
2. **所有实体、名称、数值都必须来自上方【材料片段】**，不许编造；
3. 同时给出这一页的讲稿 script：≥120 字、口语化、**要真的在讲这张图**
   （先说这张图整体在画什么，再带着听众走一遍关键路径，最后落到结论），不要念图上的字；
4. 引用材料仍用 [[c:N]]。

__DIAGRAM_SPEC__

只输出 JSON：
{"slide":{"kind":"diagram|table","title":"页面标题","bullets":["要点1","要点2"],
          "diagram":{"ir":{...}}},
 "script":"该页讲稿"}
（diagram 页给 diagram 字段；table 页改为给
 "table":{"title":"表题","columns":["列1","列2"],"rows":[["单元1","说明1"]]}）
不要输出 JSON 以外的任何文字。
"""

_GRADE_PROMPT = """你是阅卷老师。请为学生的开放题作答评分。

题目：__STEM__
参考答案：__ANSWER__
学生作答：__RESPONSE__

只输出 JSON：{"correct":true|false,"score":0~1,"feedback":"针对这份作答的点评（2~3 句）"}
评分标准：核心意思答出且无明显错误 → correct=true 且 score≥0.6；
部分正确 → correct=false 且 score 0.3~0.5；答非所问或空白 → score=0。
"""

_DEPTH_HINT = {
    "brief": "篇幅档·概览：slides 10~12 页（**少于 10 页即不合格，必须补足页数**），每页要点 ≤4 条；scripts 每段 80~150 字；cards 5~7 张；只讲最核心结论，例子最多 1 个。",
    "standard": "篇幅档·标准：slides 12~14 页（**少于 12 页即不合格，必须补足页数**），每页要点 ≤5 条；scripts 每段 150~300 字；cards 6~8 张，其中 1 张写易错点。",
    "detailed": "篇幅档·深入：slides 14~18 页（**少于 14 页即不合格，必须补足页数**），每页要点 ≤6 条；scripts 每段 300~500 字；cards 8~12 张，含推导细节与对比。",
}

# 「这一讲的材料里有没有适合画图/做表的结构」——补图兜底的前置条件。
# 只在材料确实出现结构信号时才触发补图，避免对纯叙述性材料硬造图表。
_VISUAL_SIGNALS = (
    "步骤", "流程", "顺序", "调用", "传参", "传递", "输入", "输出",
    "状态", "阶段", "切换", "变成", "转换",
    "对照", "对比", "相比", "区别", "差异", "不同",
    "两种", "三种", "几种", "分类", "类型", "层级", "结构",
)
# 至少命中这么多个不同信号才算「材料有可用对照内容」。
_MATERIAL_VISUAL_MIN = 3


def _material_looks_visual(context: str) -> bool:
    """材料片段是否含「流程 / 调用 / 状态 / 分层 / 对照」这类可视觉化结构。

    用于讲义生成后的补图兜底：整讲没有任何可视化页、且材料确实有这类结构时，
    才值得多花一次模型调用去补一张图。纯叙述性材料不触发。
    """
    t = str(context or "")
    if len(t) < 120:
        return False
    return sum(1 for w in _VISUAL_SIGNALS if w in t) >= _MATERIAL_VISUAL_MIN


# 讲次认知层级（course_lessons.depth：establish/define/derive/apply）
# 旧版缺失：讲次 depth 查 _DEPTH_HINT（brief/standard/detailed）永远 miss，恒为 standard。
_LESSON_DEPTH_HINT = {
    "establish": "讲法侧重·引入：从「为什么需要它」讲起，多打比方、建立直觉，暂不展开严格定义。",
    "define": "讲法侧重·定义：给出准确定义、符号含义与适用条件，明确「是什么、什么时候能用/不能用」。",
    "derive": "讲法侧重·推导：展示推导或论证主线，讲清每一步依据，允许较长篇幅。",
    "apply": "讲法侧重·应用：以例题或应用场景为主线，示范完整解题/使用步骤，指出易错点。",
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
    def _row_get(row: Any, key: str, default: Any = None) -> Any:
        """安全读 sqlite3.Row（列不存在时返回默认值，兼容迁移前的旧库）。"""
        try:
            return row[key]
        except (IndexError, KeyError, TypeError):
            return default

    @classmethod
    def _course_hands_mode(cls, cid: str) -> str:
        """课程的实践环节模式：``on``（含实操）/ ``off``（纯理论）/ ``auto``（按材料判断）。

        **``2`` 表示「自动」** —— 用户在建课向导里选「自动」，或旧客户端根本没传这个字段。
        列不存在（v5 之前的库）按 ``on`` 处理，保持旧行为。

        注意不要用 ``_row_get(row, "hands_on", 1)``：``bool(2)`` 虽然也是 True，但那样
        就分不出「自动」与「含实操」了。
        """
        row = get_db().query_one("SELECT hands_on FROM courses WHERE id = ?", (cid,))
        if row is None:
            return "on"
        try:
            raw = row["hands_on"]
        except (IndexError, KeyError, TypeError):
            return "on"
        if raw is None or int(raw) == _HANDS_AUTO:
            return "auto"
        return "on" if int(raw) else "off"

    @classmethod
    def _course_hands_on(cls, cid: str) -> bool:
        """该课程是否**允许**出实操题（``auto`` 也允许 —— 「自动」只是让模型按材料自己判断）。"""
        return cls._course_hands_mode(cid) != "off"

    @staticmethod
    def _hands_on_out(row: Any) -> bool | None:
        """对外输出的实践环节：``True`` 含实操 / ``False`` 纯理论 / ``None`` 自动。"""
        try:
            raw = row["hands_on"]
        except (IndexError, KeyError, TypeError):
            return True
        if raw is None or int(raw) == _HANDS_AUTO:
            return None
        return bool(raw)

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

        # 课型（主课型必选、辅助课型可选且最多一个）：既是结构模板，也是目标兜底的依据。
        # 兼容策略：**没传 intent** → 不注入课型、行为与旧版逐字一致（老客户端/旧测试）；
        # **传了但 id 非法** → 报错（说明客户端状态错了，不能静默降级成一个别的课型）。
        raw_intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else None
        intent_obj: dict[str, Any] | None = None
        if raw_intent is not None:
            primary = intent_by_id(str(raw_intent.get("primary") or ""))
            if primary is None:
                raise AppError(1000, "课型无效", "请重新选择课型后再生成大纲")
            assist = intent_by_id(str(raw_intent.get("assist") or ""))
            if assist is not None and assist["id"] == primary["id"]:
                assist = None                # 辅助课型不能与主课型相同
            intent_obj = {
                "primary": primary["id"],
                "assist": assist["id"] if assist else None,
                "note": str(raw_intent.get("note") or "").strip()[:300],
            }

        goal = str(payload.get("goal") or "").strip()
        # 目标是否由系统按课型兜底（用户一键清空）：它影响**临时标题**与**检索词**，
        # 必须与用户自己写的目标区别对待 —— 兜底句不含材料语义。
        goal_from_intent = False
        if not goal and intent_obj is not None:
            # 用户把目标一键清空时按课型兜底：目标可以留空，
            # 但检索查询与提示词里的「目的」需要一个非空文本。
            p = intent_by_id(str(intent_obj["primary"]))
            if p is not None:
                goal = f"按「{p['name']}」的方式学这门课：{p['fit']}"
                goal_from_intent = True
        if not goal:
            raise AppError(1000, "请填写学习目标", "用一句话说明「学完想做什么」")

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
        # 课程级「实践环节」开关：并非每门课都需要实操（文言文/理论课关掉即可）。
        # 实践环节三态：True = 包含实操、False = 纯理论、None / 不传 = **自动**
        # （存 2，由模型按材料判断要不要出实操题）。注意该列 NOT NULL，不能存 NULL。
        raw_hands = payload.get("hands_on")
        hands_on = _HANDS_AUTO if raw_hands is None else (1 if raw_hands else 0)

        db = get_db()
        cid = new_id()
        ts = now_iso()
        # 标题先用「目标首句」，大纲生成后由模型给出的标题覆盖。
        # 目标是课型兜底句时改优先用材料名：兜底句没有句读，取「首句」会截出
        # 「按『了解脉络型』的方…」这种半截串（用户实测很怪）。
        title = self._draft_title(goal, document_ids, prefer_material=goal_from_intent)
        db.execute(
            "INSERT INTO courses(id,title,goal,level,depth,unit_count,language,hands_on,"
            "summary,outline_json,status,error,created_at,updated_at,intent_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, title, goal[:1000], level, depth, unit_count,
             str(payload.get("language") or "zh"), hands_on,
             None, None, "drafting", None, ts, ts,
             json.dumps(intent_obj, ensure_ascii=False) if intent_obj else None),
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
        # 自定义要求：用户在「重新生成」前填写的额外说明，直接作为补充要求进提示词。
        note = str(payload.get("note") or "").strip()[:1000]
        # 单元数优先级（从「重新生成」的实际调用路径出发）：
        #   ① payload 显式给的 unit_count（含 0=自动，绝对不能被 `or` 当假值吃掉）
        #   ② note 里解析出的数量（用户实际走「重新生成」时只提交 {note}，不带 unit_count）
        #   ③ 课程旧值 row["unit_count"]（可能为 0=自动，绝不能回落到 _DEFAULT_UNITS）
        if "unit_count" in payload and payload["unit_count"] is not None:
            try:
                unit_count = int(payload["unit_count"])
            except (TypeError, ValueError):
                unit_count = int(row["unit_count"] or 0)
        else:
            unit_count = int(row["unit_count"] or 0)
        if "unit_count" not in payload or payload.get("unit_count") is None:
            note_units = _unit_count_from_note(note)
            if note_units is not None:
                unit_count = note_units
        unit_count = max(0, min(12, unit_count))

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

        # 直接喂**材料全貌**，不做语义检索。
        #
        # 原实现走 `_material(ids, "材料主题与核心内容")` —— 这个 query 对任何材料
        # 都是词汇零交集（文言文更甚），FTS 必然 0 命中；向量侧若正处本地哈希降级，
        # 打分近乎随机、可能全被 min_vec_score 过滤，兜底 `material_overview` 又只取
        # 每份文档**开头 3 片**。于是模型面对近乎空白的上下文，凭训练语料幻觉出
        # 「大模型/部署/量化」这类通用 AI 课目标（用户实测：勾《孔雀东南飞》推荐出大模型目标）。
        #
        # 推荐目标本该看整份材料的**结构**，而不是按空泛 query 抽片段——与 `_run_outline`
        # 改「概览优先喂料」是同一个教训。这里加上 chunks_per_doc=6，文言文材料也够用。
        hits, outline = get_retrieval_service().material_overview(ids, chunks_per_doc=6)
        if not hits:
            raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")
        context, _ = build_context(hits, outline=outline)

        goals: list[str] = []
        try:
            user_text = f"【学习材料概览】\n{context}"
            if len(context) < 200:
                # 材料文本极少时（扫描件、只有标题/序言），模型的幻觉风险最高，
                # 明确禁止它假设技术领域。
                user_text += ("\n\n（注意：以上材料文本非常少。目标必须直接来自上述原文，"
                              "不得假设任何技术领域、不得补充材料中没有的主题。）")
            messages = [
                {"role": "system", "content": _GOAL_PROMPT},
                {"role": "user", "content": user_text},
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

        # 把「推荐基于哪些材料」一并返回：让材料来源**永远可见**。
        # 主题错配无法程序化判定，但来源可见后用户一眼就能发现（本次 BUG 的核心诉求）。
        sources: list[str] = []
        try:
            db = get_db()
            for did in ids:
                row = db.query_one("SELECT title FROM documents WHERE id = ?", (did,))
                if not row:
                    continue
                title = str(row["title"] if isinstance(row, dict) else row[0]).strip()
                if title and title not in sources:
                    sources.append(title)
        except Exception:  # noqa: BLE001 - 来源仅用于展示，取不到不影响推荐
            sources = []
        return {"goals": goals, "sources": sources}

    # ── 课型推荐 / 按课型写目标（创建向导）───────────────
    def _material_context(self, document_ids: Sequence[str]) -> tuple[str, list[str]]:
        """材料全貌 → (提示词上下文, 来源材料标题)。

        与 `suggest_goals` 同源：喂**整份材料的概览**而不是按 query 抽片段 ——
        判断课型、写目标都该看材料的体裁与结构，按空泛 query 检索只会拿到近乎空白的
        上下文，让模型凭训练语料幻觉（曾经的「勾文言文却推荐出大模型目标」即此因）。
        """
        ids = [d for d in (document_ids or []) if d]
        if not ids:
            ids = self._ready_document_ids()
        if not ids:
            raise AppError(1002, "没有可用材料", "请先上传并解析完成至少一份文档")
        hits, outline = get_retrieval_service().material_overview(ids, chunks_per_doc=6)
        if not hits:
            raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")
        context, _ = build_context(hits, outline=outline)
        sources: list[str] = []
        db = get_db()
        for did in ids:
            row = db.query_one("SELECT title FROM documents WHERE id = ?", (did,))
            if not row:
                continue
            title = str(row["title"] if isinstance(row, dict) else row[0]).strip()
            if title and title not in sources:
                sources.append(title)
        return context, sources

    def recommend_intents(self, document_ids: Sequence[str]) -> dict[str, Any]:
        """按材料推荐**课型**（1 主 + 最多 2 备选），供创建向导预选。

        推荐只是辅助：模型失败时退回「由浅入深精读型」+ 两个通用备选，绝不报错，
        否则用户会被挡在创建流程外（与 `suggest_goals` 同样的兜底原则）。
        """
        llm = LLMClient.get_instance()
        llm.ensure_configured()
        context, sources = self._material_context(document_ids)

        live = intent_catalog()
        listing = "\n".join(f"- {t['id']}：{t['name']} —— {t['fit']}" for t in live)
        prompt = _INTENT_PICK_PROMPT.replace("__INTENT_LIST__", listing)

        primary, reason, alts = "deep-read", "", []
        try:
            raw = self._chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"【学习材料概览】\n{context}"},
                ],
                max_tokens=600,
            )
            obj = self._safe_json(raw)
            if isinstance(obj, dict):
                p = intent_by_id(str(obj.get("primary") or ""))
                if p is not None:
                    primary = p["id"]
                    reason = str(obj.get("reason") or "").strip()[:60]
                    for a in (obj.get("alternatives") or [])[:3]:
                        aid = str(a or "")
                        if intent_by_id(aid) and aid != primary and aid not in alts:
                            alts.append(aid)
        except Exception as exc:  # noqa: BLE001 - 推荐失败不该挡住创建
            logger.warning("课型推荐失败，已用默认课型",
                           extra={"extra_fields": {"type": type(exc).__name__}})
        alts = alts[:2]
        if not alts:
            alts = [t["id"] for t in live if t["id"] != primary][:2]
        return {"primary": primary, "reason": reason, "alternatives": alts, "sources": sources}

    def goal_from_intent(
        self,
        document_ids: Sequence[str],
        primary: str,
        assist: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """按「材料 + 课型」写一句**目的句**（取代原来那 4 条能力清单）。

        用户可改、可一键清空；清空后由 `create_course` 按课型兜底。
        """
        llm = LLMClient.get_instance()
        llm.ensure_configured()
        p = intent_by_id(primary)
        if p is None:
            raise AppError(1000, "课型无效", "请重新选择课型")
        context, sources = self._material_context(document_ids)
        spec = _intent_spec(p["id"], assist, note)
        user = f"{spec}\n\n【学习材料概览】\n{context}" if spec else f"【学习材料概览】\n{context}"
        goal = ""
        try:
            raw = self._chat(
                [
                    {"role": "system", "content": _GOAL_FROM_INTENT_PROMPT},
                    {"role": "user", "content": user},
                ],
                max_tokens=400,
            )
            obj = self._safe_json(raw)
            if isinstance(obj, dict):
                goal = str(obj.get("goal") or "").strip()
        except Exception as exc:  # noqa: BLE001 - 失败走确定性兜底，按钮永远有结果
            logger.warning("按课型生成目标失败，已用兜底句",
                           extra={"extra_fields": {"type": type(exc).__name__}})
        if not goal:
            goal = f"按「{p['name']}」的方式学这门课：{p['fit']}"
        return {"goal": goal[:300], "sources": sources}

    # ── 旧课程：补写讲次教学设计 desc ────────────────────
    def rebuild_desc(self, cid: str) -> dict[str, Any]:
        """为**已有旧课程**补写讲次 desc（只补 desc，不动结构、不动已生成的讲义）。

        这些课建于 desc 功能上线前（``desc_json`` 为 NULL）。用户若走「重新生成大纲」
        会重建讲次、**丢掉已经生成好的讲义** —— 所以单独给一条只补 desc 的路径。
        """
        db = get_db()
        row = db.query_one("SELECT * FROM courses WHERE id = ?", (cid,))
        if row is None:
            raise AppError(1001, "课程不存在")
        n = db.query_one(
            "SELECT COUNT(*) AS n FROM course_lessons WHERE course_id = ?", (cid,)
        )
        if not n or not int(n["n"] or 0):
            raise AppError(1003, "课程还没有讲次", "请先生成课程结构，再补写教学设计")
        job_id = self._new_job(cid, None, "outline", "正在读取课程结构")
        t = threading.Thread(target=self._run_desc_fill, args=(cid, job_id), daemon=True)
        t.start()
        return {"course_id": cid, "job_id": job_id, "status": "running"}

    def _run_desc_fill(self, cid: str, job_id: str) -> None:
        """后台线程：按现有结构为每讲补写 desc。失败只记 job，不把课程标成 failed。"""
        db = get_db()
        try:
            course = db.query_one("SELECT * FROM courses WHERE id = ?", (cid,))
            if course is None:
                raise AppError(1001, "课程不存在")
            struct: list[dict[str, Any]] = []
            lesson_rows: list[Any] = []
            for u in db.query_all(
                "SELECT * FROM course_units WHERE course_id = ? ORDER BY ordinal", (cid,)
            ):
                rows = db.query_all(
                    "SELECT * FROM course_lessons WHERE unit_id = ? ORDER BY ordinal",
                    (u["id"],),
                )
                if not rows:
                    continue
                struct.append({
                    "unit": str(u["title"]),
                    "lessons": [
                        {"lesson": str(r["title"]), "kind": str(r["kind"]),
                         "objective": str(r["objective"] or "")}
                        for r in rows
                    ],
                })
                lesson_rows.extend(rows)
            if not lesson_rows:
                raise AppError(1003, "课程还没有讲次", "请先生成课程结构，再补写教学设计")

            goal = str(course["goal"] or "")
            ids = [
                r["document_id"] for r in db.query_all(
                    "SELECT document_id FROM course_documents WHERE course_id = ?", (cid,)
                )
            ]
            self._set_stage(job_id, "正在读取学习材料")
            rs = get_retrieval_service()
            over_hits, outline = rs.material_overview(ids)
            goal_hits = _goal_hits(goal, ids)
            merged: dict[Any, dict[str, Any]] = {}
            for h in list(over_hits) + list(goal_hits):
                key = h.get("chunk_id")
                if key is not None:
                    merged.setdefault(key, h)
            hits = list(merged.values())
            if not hits:
                raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")
            context, _table = build_context(hits, outline=outline)

            self._set_stage(job_id, "正在补写教学设计")
            messages = [
                {"role": "system", "content": _DESC_FILL_PROMPT},
                {"role": "user", "content":
                    self._material_user(goal, context)
                    + "\n\n【课程结构（必须原样保持：顺序与数量都不得变）】\n"
                    + json.dumps(struct, ensure_ascii=False)},
            ]
            total = len(lesson_rows)
            filled = 0
            attempts = 0
            best = 0
            for _ in range(_MAX_RETRY + 1):
                attempts += 1
                raw = self._chat(messages, max_tokens=4096)
                # 每轮覆盖 filled；best 记录「单轮最多补上几讲」（desc 是按轮累写入库的，
                # 所以 best 才是用户真正看到已生成的数量，用它能给出准确提示）。
                filled = self._apply_desc_fill(self._safe_json(raw), lesson_rows, struct)
                best = max(best, filled)
                # 只有**全部讲次都补上**才算成功；部分成功继续重试（已写入的保留）。
                if filled == total:
                    break
                messages += [
                    {"role": "assistant", "content": (raw or "")[:1200]},
                    {"role": "user", "content":
                        "上一次输出的单元/讲次数量或顺序与输入不一致，请严格按输入的"
                        "结构重新输出，只输出一个 JSON 对象。"},
                ]
            if filled != total:
                # 重试用尽仍不全：明确报失败，不能静默「部分成功即成功」。
                # 已写入的 desc 保留（不回滚），但必须让用户知道没补全。
                logger.warning("补写教学设计未补全", extra={"extra_fields": {
                    "best": best, "total": total, "attempts": attempts}})
                self._fail_job(
                    job_id,
                    f"教学设计未补全：最多一轮补上 {best}/{total} 讲"
                    f"（共重试 {attempts} 次），请重试",
                )
                return
            self._finish_job(job_id)
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.warning("补写教学设计失败",
                           extra={"extra_fields": {"type": type(exc).__name__}})
            # 注意：**不**调用 _fail_course —— 旧课只是没补上 desc，课程本身没坏。
            self._fail_job(job_id, _err_text(exc))

    @staticmethod
    def _apply_desc_fill(parsed: Any, lesson_rows: list[Any], expected: Any = None) -> int:
        """把补写的 desc 按顺序写回讲次；结构与输入不一致时**整体拒收**。

        数量与顺序校验都是硬要求：模型少写一讲 / 把顺序打乱，后面每讲的 desc 都会
        错位挂到别的讲上 —— 那比不写更糟（会被当成「本讲本该讲这些」注入讲义提示词）。
        ``expected`` 为输入结构（单元顺序 + 每单元讲次标题序列），与模型返回的
        ``units[].lessons[].title`` **逐项比对**；不一致 → 返回 0（整体拒收，上层重试）。
        """
        if not isinstance(parsed, dict) or not isinstance(parsed.get("units"), list):
            return 0
        flat: list[Any] = []
        for u in parsed["units"]:
            if not isinstance(u, dict) or not isinstance(u.get("lessons"), list):
                return 0
            flat.extend(u["lessons"])
        if len(flat) != len(lesson_rows):
            logger.warning(
                "补写 desc 的讲次数与课程不一致，已整体拒收",
                extra={"extra_fields": {"got": len(flat), "want": len(lesson_rows)}},
            )
            return 0
        # 顺序校验：模型必须把输入的讲次标题原样回带，逐项比对（错一处即整体拒收）。
        if expected is not None:
            exp_titles: list[str] = []
            for u in expected:
                if not isinstance(u, dict):
                    return 0
                ls = u.get("lessons") or []
                if not isinstance(ls, list):
                    return 0
                for l in ls:
                    # struct 里讲次标题字段是 ``lesson``（见 _run_desc_fill 的构造）；
                    # 允许两种键名，兼容调用方。
                    t = l.get("lesson") if isinstance(l, dict) else None
                    if not t:
                        t = l.get("title") if isinstance(l, dict) else None
                    if not t:
                        return 0
                    exp_titles.append(str(t).strip())
            got_titles = [str(x.get("title") or "").strip() for x in flat]
            if len(got_titles) != len(exp_titles) or got_titles != exp_titles:
                mismatch_at = next(
                    (i for i, (a, b) in enumerate(zip(got_titles, exp_titles)) if a != b), None
                )
                logger.warning(
                    "补写 desc 的讲次**顺序/标题**与输入不一致，已整体拒收",
                    extra={"extra_fields": {
                        "got_head": got_titles[:3], "want_head": exp_titles[:3],
                        "mismatch_at": mismatch_at}},
                )
                return 0
        db = get_db()
        ts = now_iso()
        n = 0
        for item, row in zip(flat, lesson_rows):
            if not isinstance(item, dict):
                continue
            desc = _norm_desc(item.get("desc"), str(row["kind"]))
            if not desc:
                continue
            db.execute(
                "UPDATE course_lessons SET desc_json=?, updated_at=? WHERE id=?",
                (json.dumps(desc, ensure_ascii=False), ts, row["id"]),
            )
            n += 1
        return n

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
            # §1.1 overview 优先 + goal 检索补充：先拿全文章节结构与开头切片，
            # 再叠加与学习目标最相关的片段，两路 hits 合并后 build_context，
            # 让模型同时看到「材料结构」与「目标相关重点」（overview 用法照抄 _material 兜底分支）。
            rs = get_retrieval_service()
            ids = list(document_ids)
            over_hits, outline = rs.material_overview(ids)
            goal_hits = _goal_hits(goal, ids)
            merged: dict[Any, dict[str, Any]] = {}
            for h in over_hits + goal_hits:
                key = h.get("chunk_id")
                if key is not None:
                    merged.setdefault(key, h)
            hits = list(merged.values())
            if not hits:
                raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")
            context, table = build_context(hits, outline=outline)

            self._set_stage(job_id, "正在构思初步思路")
            self._set_stage(job_id, "正在构建课程结构")

            # 单元数：0 = 自动（由模型按材料体量决定），>0 = 固定个数。
            # 每单元讲次数**不固定**：内容少可以只 2 讲，内容多可以 6 讲。
            # 旧版写死「2~4 个讲次」，模型会为了凑数把一讲能讲完的内容拆薄（实测每讲仅 7~10 页）。
            lessons_rule = (
                "每个单元的讲次数由该单元的内容体量决定，**不固定**："
                "内容少可以只排 2 讲（1 讲正文 + 1 讲练习），内容多可以排到 6 讲；"
                "**严禁为了凑数量把一讲就能讲完的内容拆成两讲** —— "
                "宁可单元讲次少、每讲内容饱满"
            )
            units_rule = (
                f"单元数固定为 {unit_count} 个，{lessons_rule}" if unit_count
                else f"单元数量由你根据材料体量与学习目标自行决定（通常 2~5 个），{lessons_rule}"
            )
            # 课型（用户选定）：它决定单元切法与讲次顺序，是大纲阶段最硬的结构约束。
            # 旧课程没有 intent_json（列缺失或为 NULL）→ _intent_spec 返回空串，
            # 提示词与旧版逐字一致。
            try:
                i_row = db.query_one("SELECT intent_json FROM courses WHERE id = ?", (cid,))
                i_obj = json.loads((i_row["intent_json"] if i_row else None) or "{}")
                if not isinstance(i_obj, dict):
                    i_obj = {}
            except Exception:  # noqa: BLE001 - 脏数据/旧库按「无课型」处理
                i_obj = {}
            intent_spec = _intent_spec(
                str(i_obj.get("primary") or ""),
                str(i_obj.get("assist") or "") or None,
                str(i_obj.get("note") or "")[:300],
            )

            prompt = (
                _OUTLINE_PROMPT
                .replace("__UNITS_RULE__", units_rule)
                .replace("__LESSONS_RULE__", lessons_rule)
                .replace("__DESC_SPEC__", _DESC_SPEC)
                .replace("__INTENT_SPEC__", intent_spec)
            ) + (
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
                # note 里可能写「五章」而本课程又固定了单元数，两条指令会冲突——
                # 显式声明以固定单元数为准，避免模型被 note 带偏（note 为空不改文案）。
                if unit_count >= 1:
                    prompt += "（若上面的要求与本课程固定的单元数冲突，以单元数为准）"
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

            # B 防线（大纲 vs 材料对齐校验）：desc 是后续所有讲义共同上游，
            # 大纲对材料的误解会被一致放大到每一讲 → 出纲后核一遍再落库。
            # 非阻塞：校验调用失败/超时一律静默跳过，不挡出纲。
            self._set_stage(job_id, "正在核对大纲与材料")
            obj = self._align_outline(obj, context)

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
                # §2.2 禁词校验：objective 前 12 字含「了解/熟悉/掌握/学习」→ 判不合规，
                # 触发既有重试链（返回 None 让 _run_outline 走重试 / 兜底）。
                l_obj = str(l.get("objective") or "").strip()[:300]
                if any(w in l_obj[:12] for w in ("了解", "熟悉", "掌握", "学习")):
                    return None
                # §1.2 depth 归一到四值枚举：establish|define|derive|apply，
                # 未知值（含脏值、课程级 brief/standard/detailed 等）一律默认 define，不透传脏值。
                depth_raw = str(l.get("depth") or "").strip().lower()
                depth = depth_raw if depth_raw in ("establish", "define", "derive", "apply") else "define"
                # desc 缺失不算失败（旧链路兼容），但给了就清洗归一化，
                # 练习讲/正文讲按 kind 各自校验字段集。
                desc = _norm_desc(l.get("desc"), kind)
                lessons.append({
                    "title": l_title[:120],
                    "objective": l_obj,
                    "kind": kind,
                    "depth": depth,
                    "desc": desc,
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

    def _align_outline(self, obj: dict[str, Any], context: str) -> dict[str, Any]:
        """大纲 vs 材料对齐校验：把 desc 里编造的知识点拉回材料（只修 desc，不动结构）。

        任何失败（解析不了/超限/字段非法）都静默返回原大纲——这是增强校验，
        不能反过来成为出纲的新故障点。
        """
        try:
            slim = [
                {"unit": i, "title": u["title"],
                 "lessons": [
                     {"lesson": j, "title": l["title"], "kind": l["kind"],
                      "desc": l.get("desc")}
                     for j, l in enumerate(u["lessons"], 1)
                 ]}
                for i, u in enumerate(obj["units"], 1)
            ]
            raw = self._chat([
                {"role": "system", "content": _ALIGN_PROMPT},
                {"role": "user", "content": "【大纲】\n"
                 + json.dumps(slim, ensure_ascii=False)
                 + "\n\n【材料片段】\n" + context[:3000]},
            ], max_tokens=2048)
            parsed = self._safe_json(raw)
            if not isinstance(parsed, dict) or parsed.get("ok") is not False:
                return obj
            fixes = parsed.get("fixes")
            if not isinstance(fixes, list):
                return obj
            applied = skipped = 0
            for f in fixes:
                if not isinstance(f, dict):
                    continue
                ui, li = f.get("unit"), f.get("lesson")
                # 严格整数（bool 也是 int 子类，要排除）：float 2.5 静默截成 2 会修错讲
                if not isinstance(ui, int) or isinstance(ui, bool) \
                        or not isinstance(li, int) or isinstance(li, bool):
                    skipped += 1
                    continue
                if not (1 <= ui <= len(obj["units"])):
                    skipped += 1
                    continue
                lessons = obj["units"][ui - 1]["lessons"]
                if not (1 <= li <= len(lessons)):
                    skipped += 1
                    continue
                nd = _norm_desc(f.get("desc"), str(lessons[li - 1]["kind"]))
                if not nd:
                    skipped += 1
                    continue
                # 按字段 merge：模型在 fix 里漏写的字段保留原值（整包替换会抹字段）
                old = lessons[li - 1].get("desc")
                if isinstance(old, dict):
                    merged = dict(old)
                    merged.update(nd)
                    nd = merged
                lessons[li - 1]["desc"] = nd
                applied += 1
            if applied:
                logger.info(
                    "大纲对齐校验修正 %d 讲（跳过非法 fix %d 条）", applied, skipped,
                    extra={"extra_fields": {"type": "outline_align"}},
                )
            return obj
        except Exception as exc:  # noqa: BLE001 - 非阻塞防线（但留痕备查）
            logger.warning(
                "大纲对齐校验失败，已静默跳过：%s", type(exc).__name__,
                extra={"extra_fields": {"type": "outline_align"}},
            )
            return obj

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
                    desc = lesson.get("desc")
                    db.execute(
                        "INSERT INTO course_lessons(id,course_id,unit_id,ordinal,global_ordinal,"
                        "kind,title,objective,depth,status,created_at,updated_at,desc_json)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            new_id(), cid, uid, l_idx, global_ordinal,
                            lesson["kind"], lesson["title"], lesson.get("objective") or "",
                            lesson.get("depth") or "standard", "pending", ts, ts,
                            json.dumps(desc, ensure_ascii=False) if desc else None,
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
        # 是否带讲次 id（新前端格式）。任一讲次带 id 即按 id 复用行。
        # 整批都不带 id = 老客户端/旧测试 → 退回原「按 (unit_id, ordinal) 位置复用」，
        # 保持逐字兼容（Z7e 兼容断言依赖「无 id → 行为与旧版一致」）。
        has_ids = any(
            isinstance(u, dict) and isinstance(u.get("lessons"), list)
            and any(isinstance(lv, dict) and lv.get("id") for lv in u["lessons"])
            for u in units
        )
        # 课程级 kept_ids：支持讲次**跨单元移动**（内容跟着讲次走）。
        # 删除必须放在所有 UPDATE 之后、且按「course_id + id NOT IN」整课执行，
        # 否则从单元 A 移动到单元 B 的讲次会被 A 的「单元内删除」误删（它此刻仍挂在 A 下）。
        kept_ids: list[str] = []
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
                lessons_raw = list(lessons_raw)[:12]
                # F3/F4 防线：讲次标题为空（含纯空格）→ **整批拒绝**，绝不静默跳过。
                # 静默跳过会让该讲次不进 kept_ids，最终被课程级删除误删（数据灾难边界）。
                # 注意：lessons_raw 为空数组（用户主动删光该单元讲次）是合法操作，不拦。
                for _lk in lessons_raw:
                    if isinstance(_lk, dict) and not str(_lk.get("title") or "").strip():
                        raise AppError(1000, "讲次标题不能为空", "请补全标题后再保存")
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

                for l_idx, lesson in enumerate(lessons_raw, start=1):
                    if not isinstance(lesson, dict):
                        continue
                    l_title = str(lesson.get("title") or "").strip()[:120]
                    if not l_title:
                        continue
                    kind = str(lesson.get("kind") or "lecture").strip().lower()
                    if kind not in ("lecture", "practice", "project"):
                        kind = "lecture"
                    objective = str(lesson.get("objective") or "").strip()[:300]
                    l_row = None
                    if has_ids and lesson.get("id"):
                        # 带 id 的新前端：按讲次 id 复用行（内容字段跟着讲次走，支持跨单元移动）。
                        # 必须校验 course_id 归属，防跨课程 id 注入。
                        l_row = db.query_one(
                            "SELECT id FROM course_lessons WHERE id = ? AND course_id = ?",
                            (str(lesson["id"]), cid),
                        )
                        if l_row is not None:
                            kept_ids.append(str(lesson["id"]))
                    if l_row is None and not (has_ids and lesson.get("id")):
                        # 无 id（老前端）或该 id 不属于本课程 → 按位置复用 / 当新增处理。
                        l_row = db.query_one(
                            "SELECT id, course_id FROM course_lessons WHERE unit_id = ? AND ordinal = ?",
                            (uid, l_idx),
                        )
                        if l_row is not None and has_ids:
                            # 带 id 的 payload 里出现「无 id 或跨课程 id」的项：
                            # 只有该行确属本课程才允许按位置复用（防御，正常前端不会走到）。
                            l_own = db.query_one(
                                "SELECT id FROM course_lessons WHERE id=? AND course_id=?",
                                (l_row["id"], cid),
                            )
                            if l_own is not None:
                                kept_ids.append(str(l_row["id"]))
                    if l_row is None:
                        # 新增行：INSERT（沿用现有 INSERT 语句与 desc_json 处理）。
                        # 新行 id 必须纳入课程级 kept_ids，否则事务末尾的
                        # DELETE id NOT IN (kept_ids) 会把本轮新增的讲次一并删掉。
                        l_desc = _norm_desc(lesson.get("desc"), kind)
                        lid = new_id()
                        kept_ids.append(lid)
                        db.execute(
                            "INSERT INTO course_lessons(id,course_id,unit_id,ordinal,"
                            "global_ordinal,kind,title,objective,depth,status,created_at,"
                            "updated_at,desc_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (lid, cid, uid, l_idx, u_idx * 100 + l_idx, kind, l_title,
                             objective, str(lesson.get("depth") or "standard"), "pending",
                             ts, ts, json.dumps(l_desc, ensure_ascii=False) if l_desc else None),
                        )
                    elif "desc" in lesson:
                        # desc 必须跟着**讲次对象**走，不能跟着位置走：编辑结构（删一讲、加两讲）时
                        # 前端会把每个讲次自己的 desc 一并提交；若沿用「按序号位置复用行」的做法，
                        # 被删讲次之后的所有讲次都会**继承前一讲的 desc**（静默错位，比没有 desc 更糟
                        # —— 它会作为「本讲教学设计」注入讲义提示词，把内容约束到错误的边界上）。
                        # payload 里没有 desc 键 → 不动旧值（兼容老前端）；显式 null → 清空。
                        l_desc = _norm_desc(lesson.get("desc"), kind)
                        db.execute(
                            "UPDATE course_lessons SET unit_id=?, ordinal=?, title=?, objective=?,"
                            " kind=?, desc_json=?, updated_at=? WHERE id=?",
                            (uid, l_idx, l_title, objective, kind,
                             json.dumps(l_desc, ensure_ascii=False) if l_desc else None,
                             ts, l_row["id"]),
                        )
                    else:
                        # 覆盖结构字段（含 unit_id/ordinal：支持讲次跨单元移动，内容跟着讲次走），
                        # 不动任何内容字段（board/slides/scripts/marks/citations/conversation_id/status）。
                        db.execute(
                            "UPDATE course_lessons SET unit_id=?, ordinal=?, title=?, objective=?,"
                            " kind=?, updated_at=? WHERE id=?",
                            (uid, l_idx, l_title, objective, kind, ts, l_row["id"]),
                        )
                # 老前端（整批无 id）：保留原「按位置删多余讲次」行为。
                # 带 id 时**不在单元内删除** —— 用户删除/移动的讲次统一交给课程级删除
                #（见事务末尾），否则跨单元移动的讲次会在旧单元被误删。
                if not has_ids:
                    db.execute(
                        "DELETE FROM course_lessons WHERE unit_id = ? AND ordinal > ?",
                        (uid, len(lessons_raw)),
                    )
            # 删除多余的旧单元
            db.execute("DELETE FROM course_units WHERE course_id = ? AND ordinal > ?",
                       (cid, len(units)))
            if has_ids:
                # 双保险：用户提交了非空讲次，但一个都没被接受（kept_ids 为空）→
                # 绝不退化成「删光整课」。整批拒绝，课程保持原状（异常触发事务回滚）。
                # 选择「报错」而非「跳过删除」：让异常结构（空 title / 跨课程 id 注入 /
                # 其它不可预期的失配）以明确错误返回给客户端，而不是静默保留陈旧数据，
                # 也避免「提交看似成功、实则什么都没改」的误导。
                # 注：kept_ids 为空且**未提交任何讲次** = 用户主动删光全部讲次（合法），走下方全删。
                _submitted = sum(
                    len(u.get("lessons") or []) for u in units if isinstance(u, dict)
                )
                if _submitted > 0 and not kept_ids:
                    raise AppError(
                        1000, "课程结构无法落到现有讲次",
                        "请刷新页面后重试，不要提交空标题或异常结构",
                    )
                # 课程级删除未保留的讲次：kept_ids 为空 = 本轮全删（用户主动清空全部讲次）。
                # 用 placeholders 安全拼接（id 来自新前端，逐项 bind）。
                if kept_ids:
                    ph = ",".join("?" for _ in kept_ids)
                    db.execute(
                        f"DELETE FROM course_lessons WHERE course_id = ? AND id NOT IN ({ph})",
                        (cid, *kept_ids),
                    )
                else:
                    db.execute("DELETE FROM course_lessons WHERE course_id = ?", (cid,))
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
            course_row = dict(get_db().query_one(
                "SELECT goal, level, depth FROM courses WHERE id = ?", (lesson["course_id"],)
            ) or {})   # query_one 返回 sqlite3.Row：没有 .get，先转 dict
            unit_title = self._unit_title(lesson["unit_id"])
            goal = (course_row or {}).get("goal") or ""
            # §1.3 recall：检索 query 扩为 title + objective + 单元标题 + 课程目标
            query = " ".join(x for x in (lesson["title"], lesson["objective"], unit_title, goal) if x)
            hits, context, table = self._material(document_ids, query, top_k=_MAX_HITS)
            if not hits:
                raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")

            self._set_stage(job_id, "正在生成讲义内容")
            # §1.2/§2.4：__DEPTH__ 改为「讲法侧重 + 篇幅档」正交拼接
            lesson_hint = _LESSON_DEPTH_HINT.get(str(lesson.get("depth") or "").strip(), "")
            volume_hint = _DEPTH_HINT.get(
                str((course_row or {}).get("depth") or "standard").strip() or "standard",
                _DEPTH_HINT["standard"],
            )
            depth_block = "；".join(x for x in (lesson_hint, volume_hint) if x)
            hands_mode = self._course_hands_mode(lesson["course_id"])
            if hands_mode == "off":
                # 课程级关闭实操：防止文科/理论课的讲稿里冒出「你现在打开终端试试」。
                depth_block += ("；本课程为理论型课程，课件与讲稿**不得布置真实操作任务**"
                                "（不要出现「打开终端/运行命令/动手试一下」这类指令）")
            elif hands_mode == "auto":
                # 「自动」：把判断交回模型（操作性材料才布置动手任务，纯概念别硬造）
                depth_block += ("；是否布置动手任务**由你按材料判断**："
                                "材料以操作流程/命令/步骤为主时可安排，纯概念材料不要硬造")
            prompt = (
                _LECTURE_PROMPT
                .replace("__DEPTH__", depth_block)
                .replace("__TITLE__", lesson["title"])
                .replace("__OBJECTIVE__", lesson["objective"] or lesson["title"])
                .replace("__UNIT__", unit_title)
                .replace("__DESC__", self._desc_block(lesson))
                .replace("__SIBLINGS__", self._siblings_block(
                    lesson["course_id"], lesson["unit_id"], lesson_id))
                .replace("__DIAGRAM_SPEC__", diagram_mod.prompt_spec())
            )
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": self._lesson_user(lesson, query, context)},
            ]

            obj: dict[str, Any] | None = None
            for _ in range(_MAX_RETRY + 1):
                raw = self._chat(messages, max_tokens=8192)
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
            else:
                # 结构校验通过 ≠ 讲稿合格：再拦一次「照着课件念」。
                # 这条用文本相似度做结构判定，不依赖模型自觉。
                obj = self._repair_mirrored_scripts(obj, job_id)

            # §补图兜底：整讲一页可视化都没有、但材料有可视觉化结构 → 补一页
            # （必须在编译之前：补出来的页要一起走校验/重写/退化链路）
            obj = self._ensure_visuals(
                obj, job_id, context, lesson["title"], lesson["objective"] or lesson["title"]
            )
            # §Archify 管线：先把 diagram 页的 IR 编译成 SVG
            # （校验 → 带回执定向重写一次 → 仍失败则退化为要点页），再走统一的净化与限页。
            obj = self._compile_diagrams(obj, job_id)
            # §可视化契约：落库前净化 diagram/chart（限页数、剥非法字段、数字转 float）
            obj = self._sanitize_visuals(obj)
            # §跨讲去重护栏：落库前用同课程其他讲次已落库课件标题做重复拦截（非阻塞）。
            obj = self._dedup_lecture(
                lesson["course_id"], lesson_id, obj,
                set_stage=lambda msg: self._set_stage(job_id, msg),
            )
            board, citations = self._resolve_board(obj, table)
            # §四 观察点（第一条）：检索有命中但讲义未引用任何材料 → 记录，不阻断
            if not citations and hits:
                logger.info(
                    "讲义落库但引用为空（检索有命中，模型未引用材料）",
                    extra={"extra_fields": {"lesson": lesson_id, "hits": len(hits)}},
                )
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
            # 预生成：优先推进「整章」队列；没有队列任务时按自动模式把本讲练习备出来。
            if not self._pump_prefetch(lesson["course_id"]):
                self._queue_prefetch(lesson["course_id"], lesson_id, "practice")
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

    # ── 可视化契约：diagram / chart 的落库前净化 ─────────────────────
    # 前端（visual.js）用 mermaid.js / ECharts 确定性渲染，这里只做**粗校验**：
    # 语法级校验由前端 parse 兜底，前端失败还会回退要点页 —— 三层防线：
    # 提示词约束 → 这里剥坏字段 → 前端渲染失败回退。任何一层失败都不影响整讲。
    _MERMAID_STARTS = ("flowchart", "graph", "sequenceDiagram",
                       "stateDiagram-v2", "stateDiagram", "classDiagram")
    _MERMAID_BANNED = ("classDef", "linkStyle", "%%")

    def _sanitize_visuals(self, obj: dict[str, Any]) -> dict[str, Any]:
        """净化模型产出的可视化页（限页数、剥非法字段、数字转 float）。

        非法字段直接剥掉并把 kind 退回 note —— 页面保留 title/bullets，
        学生永远看不到空白或报错。合法的 chart.data 统一转 float 落库。
        """
        slides = obj.get("slides") if isinstance(obj, dict) else None
        if not isinstance(slides, list):
            return obj
        for sl in slides:
            if not isinstance(sl, dict):
                continue
            kind = str(sl.get("kind") or "")
            if kind == "diagram":
                if not self._valid_diagram(sl.get("diagram")):
                    sl.pop("diagram", None)
                    sl["kind"] = "note"
            elif kind == "chart":
                if not self._valid_chart(sl.get("chart")):
                    sl.pop("chart", None)
                    sl["kind"] = "note"
            elif kind == "table":
                if not self._valid_table(sl.get("table")):
                    sl.pop("table", None)
                    sl["kind"] = "note"
                    logger.info("非法 table 页已退化为要点页",
                                extra={"extra_fields": {"id": sl.get("id")}})
            elif kind == "takeaway":
                raw = sl.get("takeaway")
                if not self._valid_takeaway(raw):
                    sl.pop("takeaway", None)
                    sl["kind"] = "note"
                else:
                    sl["takeaway"] = str(raw).strip()[:60]
        # 页数上限：diagram/chart/table 合计 ≤2，从后往前剥（takeaway 是文字形态，
        # 不计入该上限，单独限 1 页）
        visual_kinds = ("diagram", "chart", "table")
        visual_idx = [i for i, sl in enumerate(slides)
                      if isinstance(sl, dict) and sl.get("kind") in visual_kinds
                      and any(sl.get(k) for k in visual_kinds)]
        for i in reversed(visual_idx[2:]):
            sl = slides[i]
            for k in visual_kinds:
                sl.pop(k, None)
            sl["kind"] = "note"
            logger.info("可视化页超限已剥除", extra={"extra_fields": {"index": i}})
        tk_idx = [i for i, sl in enumerate(slides)
                  if isinstance(sl, dict) and sl.get("kind") == "takeaway" and sl.get("takeaway")]
        for i in reversed(tk_idx[1:]):
            slides[i].pop("takeaway", None)
            slides[i]["kind"] = "note"
            logger.info("takeaway 页超限已剥除", extra={"extra_fields": {"index": i}})
        return obj

    @classmethod
    def _valid_diagram(cls, diagram: Any) -> bool:
        """diagram 字段合法性：新形态（编译产物 svg + ir）或旧形态（mermaid code）任一生效即可。

        旧形态是**向后兼容**用的：库里早先落下的讲义存的是 mermaid 源码，
        前端仍走 mermaid 渲染路径，不能被这次升级判成坏字段。
        """
        if not isinstance(diagram, dict):
            return False
        svg = diagram.get("svg")
        if isinstance(svg, str) and svg.lstrip().startswith("<svg"):
            return True
        return cls._valid_mermaid(diagram)

    def _ensure_visuals(self, obj: dict[str, Any], job_id: str, context: str,
                        title: str, objective: str) -> dict[str, Any]:
        """补图兜底：整讲没有可视化页、但材料确实有可视觉化结构时，补一页。

        为什么需要它：提示词里的「用图/用表」无论写得多硬，模型都可能整讲不画
        （实测同一材料连跑三次，图示页数 2 / 0 / 1 —— 画不画全看运气）。
        所以除了提示词约束，再加一道**服务端兜底**：
        讲义落库前检查一次，该有图却没有 → 让模型只补这一页。
        补出来的页同样会走 `_compile_diagrams` 的校验/重写/退化链路，不会放过坏图。
        """
        slides = obj.get("slides")
        if not isinstance(slides, list) or not slides:
            return obj
        has_visual = any(
            isinstance(sl, dict)
            and (sl.get("diagram") or sl.get("chart") or sl.get("table"))
            for sl in slides
        )
        if has_visual:
            return obj
        if not _material_looks_visual(context):
            logger.info("整讲无可视化页，但材料也没有可视觉化结构，不补图",
                        extra={"extra_fields": {"slides": len(slides)}})
            return obj
        brief = "\n".join(
            f"- {(sl.get('title') or '')}｜" + "；".join((sl.get("bullets") or [])[:3])
            for sl in slides[:12] if isinstance(sl, dict)
        )
        try:
            self._set_stage(job_id, "本讲缺少图示，正在补一张图")
            prompt = (
                _VISUAL_ADD_PROMPT
                .replace("__TITLE__", title)
                .replace("__OBJECTIVE__", objective)
                .replace("__SLIDES__", brief[:2500])
                .replace("__CONTEXT__", str(context or "")[:6000])
                .replace("__DIAGRAM_SPEC__", diagram_mod.prompt_spec())
            )
            raw = self._chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "请只补这一页可视化，输出 JSON。"},
                ],
                max_tokens=2000,
            )
            parsed = self._safe_json(raw) or {}
            cand = parsed.get("slide") if isinstance(parsed, dict) else None
            if not isinstance(cand, dict):
                return obj
            kind = str(cand.get("kind") or "").strip().lower()
            title = str(cand.get("title") or "").strip()
            bullets = [str(x).strip()[:60] for x in (cand.get("bullets") or []) if str(x).strip()][:3]
            if kind not in ("diagram", "table") or not title or not bullets:
                logger.info("补图结果不合规，跳过", extra={"extra_fields": {"kind": kind}})
                return obj
            new_slide: dict[str, Any] = {
                "id": "slide-visual-added",
                "kind": kind,
                "title": title[:120],
                "bullets": bullets,
                "body": "",
                "citation_refs": [],
            }
            if kind == "diagram":
                dg = cand.get("diagram")
                ir = dg.get("ir") if isinstance(dg, dict) else None
                if not isinstance(ir, dict):
                    logger.info("补图未给出可用 IR，跳过")
                    return obj
                new_slide["diagram"] = {"ir": ir}
            else:
                tb = cand.get("table")
                if not isinstance(tb, dict) or not self._valid_table(tb):
                    logger.info("补图表格不合法，跳过")
                    return obj
                new_slide["table"] = tb
            # 插在小结之前（没有小结就追加到末尾），并同步插入讲稿保持逐页对应
            idx = len(slides) - 1 if len(slides) >= 3 else len(slides)
            slides.insert(idx, new_slide)
            scripts = obj.get("scripts")
            text = str(parsed.get("script") or "").strip()
            # 引用编号从讲稿里真实提取（不要凭猜，也不要从 marks 抄）
            new_slide["citation_refs"] = sorted(
                {int(x) for x in re.findall(r"\[\[c:(\d+)\]\]", text)}
            )
            if isinstance(scripts, list):
                if not text:
                    # 模型没给这一页讲稿 → 用**确定性方法**补一版，保证「课件页 ⇄ 讲稿」
                    # 严格一一对应：否则多出来的那页课件没有讲稿，导出 / Markdown 的
                    # 「第 i 页 · 讲稿」会整体错位（前端能降级显示，但存库 JSON 本身是残的）。
                    text = self._script_from_slide(new_slide)
                if text:
                    scripts.insert(min(idx, len(scripts)),
                                   {"slide_id": new_slide["id"], "text": text[:2000]})
            logger.info("已补 1 页可视化", extra={"extra_fields": {
                "kind": kind, "index": idx, "slides": len(slides)}})
        except Exception as exc:  # noqa: BLE001 - 补图失败不能影响整讲
            logger.warning("补图失败", extra={"extra_fields": {"type": type(exc).__name__}})
        return obj

    def _compile_diagrams(self, obj: dict[str, Any], job_id: str) -> dict[str, Any]:
        """把 diagram 页的 IR 编译成 SVG —— Archify 的四段结构。

        **校验 → 带回执定向重写一次 → 仍不合格则剥字段退化为要点页**，
        与讲稿「雷同判定 → 定向重写 → 确定性兜底」是同一个范式：
        不信任模型一次就写对，但也不让它把整页拖垮 —— 学生永远看不到空白或坏图。
        """
        slides = obj.get("slides")
        if not isinstance(slides, list):
            return obj
        for sl in slides:
            if not isinstance(sl, dict) or str(sl.get("kind") or "") != "diagram":
                continue
            dg = sl.get("diagram")
            if not isinstance(dg, dict):
                continue
            if isinstance(dg.get("svg"), str) and dg["svg"].lstrip().startswith("<svg"):
                continue                        # 已是编译产物（重放/迁移数据）
            ir = dg.get("ir")
            if not isinstance(ir, dict):
                # 旧形态（模型偶发仍写 mermaid 代码）：交给 _valid_mermaid 老路径判定
                continue
            svg, receipt = diagram_mod.compile_ir(ir)
            if svg is None:
                logger.info("图示 IR 未通过校验", extra={"extra_fields": {
                    "slide": sl.get("id"), "stage": receipt.get("stage"),
                    "codes": [d.get("code") for d in (receipt.get("diagnostics") or [])][:4]}})
                self._set_stage(job_id, "图示需要修正，正在按诊断重写")
                fixed = self._rewrite_diagram(ir, receipt)
                if fixed is not None:
                    svg, _ = diagram_mod.compile_ir(fixed)
                    if svg is not None:
                        ir = fixed
            if svg is None:
                sl.pop("diagram", None)
                sl["kind"] = "note"
                logger.info("图示重写后仍不合格，已退化为要点页",
                            extra={"extra_fields": {"slide": sl.get("id")}})
                continue
            sl["diagram"] = {
                "ir": ir,
                "svg": svg,
                "preset": str((ir.get("meta") or {}).get("preset") or "classic"),
                "diagram_type": str(ir.get("diagram_type") or ""),
            }
        return obj

    def _rewrite_diagram(self, ir: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any] | None:
        """带着修复回执重写一次图示 IR；模型没给出可用结果时返回 None。"""
        diags = json.dumps(receipt.get("diagnostics") or [], ensure_ascii=False)[:2000]
        fixes = json.dumps(receipt.get("supportedFixes") or [], ensure_ascii=False)[:1200]
        try:
            prompt = (
                _DIAGRAM_REWRITE_PROMPT
                .replace("__DIAGNOSTICS__", diags)
                .replace("__FIXES__", fixes)
                .replace("__IR__", json.dumps(ir, ensure_ascii=False)[:3000])
            )
            raw = self._chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "请只修正被点名的对象，输出修正后的 IR。"},
                ],
                max_tokens=1600,
            )
            parsed = self._safe_json(raw) or {}
            cand = parsed.get("diagram") if isinstance(parsed, dict) else None
            inner = cand.get("ir") if isinstance(cand, dict) else None
            return inner if isinstance(inner, dict) else None
        except Exception as exc:  # noqa: BLE001 - 重写失败就走兜底，不能影响整讲
            logger.warning("图示重写失败", extra={"extra_fields": {"type": type(exc).__name__}})
            return None

    @classmethod
    def _valid_mermaid(cls, diagram: Any) -> bool:
        """diagram 字段粗校验：图型白名单开头、≤30 行、无样式/指令语句。"""
        if not isinstance(diagram, dict):
            return False
        code = diagram.get("code")
        if not isinstance(code, str) or not code.strip():
            return False
        if str(diagram.get("lang") or "mermaid") != "mermaid":
            return False
        text = code.strip()
        if not text.startswith(cls._MERMAID_STARTS):
            return False
        if len(text.splitlines()) > 30:
            return False
        if any(b in text for b in cls._MERMAID_BANNED):
            return False
        if re.search(r"(?m)^\s*style\s", text):
            return False
        return True

    @staticmethod
    def _valid_chart(chart: Any) -> bool:
        """chart 字段粗校验：类型白名单、数值有限且等长、类目/系列上限。"""
        if not isinstance(chart, dict):
            return False
        ctype = str(chart.get("type") or "").strip().lower()
        if ctype not in ("bar", "line", "pie"):
            return False
        cats = chart.get("categories")
        series = chart.get("series")
        if not isinstance(cats, list) or not cats or len(cats) > 12:
            return False
        if not isinstance(series, list) or not series:
            return False
        if ctype == "pie" and len(series) != 1:
            return False
        if len(series) > 2:
            return False
        for s in series:
            if not isinstance(s, dict):
                return False
            data = s.get("data")
            if not isinstance(data, list) or len(data) != len(cats):
                return False
            clean: list[float] = []
            for v in data:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return False
                if not math.isfinite(v):
                    return False
                clean.append(float(v))
            s["data"] = clean
            if "name" in s:
                s["name"] = str(s["name"])[:60]
        chart["type"] = ctype
        if "unit" in chart:
            chart["unit"] = str(chart.get("unit") or "")[:20]
        return True

    @staticmethod
    def _valid_table(table: Any) -> bool:
        """table 字段粗校验 + 原地规范化：2~5 列、1~8 行、单元格 ≤40 字。

        合法时把列名与单元格统一截断写回，保证前端只面对干净数据；
        非法（列数越界、行列不齐、全空）返回 False，调用方把该页退化为要点页。
        """
        if not isinstance(table, dict):
            return False
        cols = table.get("columns")
        rows = table.get("rows")
        if not isinstance(cols, list) or not (2 <= len(cols) <= 5):
            return False
        if not isinstance(rows, list) or not rows or len(rows) > 8:
            return False
        clean_cols = [str(c).strip()[:40] for c in cols]
        if any(not c for c in clean_cols):
            return False
        clean_rows: list[list[str]] = []
        for r in rows:
            if not isinstance(r, list) or len(r) != len(clean_cols):
                return False
            clean_rows.append([str(cell).strip()[:40] for cell in r])
        if not any(any(cell for cell in r) for r in clean_rows):
            return False
        table["columns"] = clean_cols
        table["rows"] = clean_rows
        if "title" in table:
            table["title"] = str(table.get("title") or "")[:60]
        return True

    @staticmethod
    def _valid_takeaway(value: Any) -> bool:
        """takeaway 字段粗校验：必须是非空字符串（超长由调用方截断到 60 字）。"""
        return isinstance(value, str) and bool(value.strip())


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
            if kind not in ("concept", "example", "formula", "quote", "note",
                            "diagram", "chart", "table", "takeaway"):
                kind = "note"
            refs: list[int] = []
            for n in item.get("citation_refs") or []:
                try:
                    iv = int(n)
                except (TypeError, ValueError):
                    continue
                if iv > 0 and iv not in refs:
                    refs.append(iv)
            # §可视化契约：diagram/chart 字段原样保留，供前端按字段渲染
            # （_sanitize_visuals 在落库前再做合法性与页数校验）。
            slide: dict[str, Any] = {
                "id": sid,
                "kind": kind,
                "title": title[:120] or "课件页",
                "bullets": bullets[:8],
                "body": body[:900],
                "citation_refs": refs[:8],
            }
            if kind == "diagram" and isinstance(item.get("diagram"), dict):
                slide["diagram"] = item["diagram"]
            elif kind == "chart" and isinstance(item.get("chart"), dict):
                slide["chart"] = item["chart"]
            elif kind == "table" and isinstance(item.get("table"), dict):
                slide["table"] = item["table"]
            elif kind == "takeaway" and isinstance(item.get("takeaway"), str):
                slide["takeaway"] = item["takeaway"]
            slides.append(slide)
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
    def _script_mirrors_slide(slide: dict[str, Any], text: str) -> bool:
        """讲稿是否只是把课件页逐字念了一遍（确定性判定，不问模型）。

        三条判据命中任一即算复述：

        1. 归一化后的课件文本几乎完整出现在讲稿里（课件 ≥12 字且被包含）；
        2. ``difflib`` 相似度 ≥ :data:`_SCRIPT_DUP_RATIO`；
        3. 相似度 ≥ :data:`_SCRIPT_DUP_SOFT_RATIO` 且讲稿长度不足课件的
           :data:`_SCRIPT_MIN_EXPAND` 倍 —— 既没换说法也没展开。

        Args:
            slide: 课件页（``title`` / ``bullets`` / ``body``）。
            text: 对应的讲师讲稿原文。

        Returns:
            判定为「照念课件」时返回 ``True``。
        """
        slide_text = _norm_text(CourseService._script_from_slide(slide))
        script_text = _norm_text(text)
        if not slide_text or not script_text:
            return False
        if len(slide_text) >= 12 and slide_text in script_text:
            return True
        ratio = difflib.SequenceMatcher(None, slide_text, script_text).ratio()
        if ratio >= _SCRIPT_DUP_RATIO:
            return True
        return ratio >= _SCRIPT_DUP_SOFT_RATIO and len(script_text) < len(slide_text) * _SCRIPT_MIN_EXPAND

    @staticmethod
    def _expand_script(slide: dict[str, Any]) -> str:
        """把课件页确定性扩写成一段「不像念 PPT」的讲稿（最后一道兜底）。

        只在模型两次都写成复述时启用。用固定话术框架把课件要点转成口语化
        讲解，保证产出与课件原文不重合，也保证课堂永远不会没词可说。

        Args:
            slide: 课件页。

        Returns:
            讲稿文本（≤4000 字）。
        """
        title = str(slide.get("title") or "这一页").strip()
        bullets = [str(x).strip() for x in (slide.get("bullets") or []) if str(x).strip()]
        body = str(slide.get("body") or "").strip()
        lines = [f"我们来看这一页，主题是「{title}」。"]
        if bullets:
            lines.append("先把这里的要点串一遍：" + "；".join(bullets) + "。")
            lines.append(
                f"为什么要放在一起看？你可以先想一想：这一步在解决什么问题、"
                f"如果不这么做会卡在哪里。想清楚这一点，再往下看会顺很多。"
            )
        if body:
            lines.append("再补充一点：" + body)
        lines.append("这一页就先讲到这里，我们接着往下看。")
        return "".join(lines)[:4000]

    def _repair_mirrored_scripts(
        self, obj: dict[str, Any], job_id: str
    ) -> dict[str, Any]:
        """拦下「讲稿逐字照念课件」：先定向重写一次，仍不合格则确定性扩写。

        与引导式护栏同一思路 —— **不信任模型的自述**，用文本相似度做结构判定；
        违规就重试，再不行走确定性兜底，保证交付出去的讲稿一定不是复述。

        Args:
            obj: 已通过结构校验的课堂内容包（含 ``slides`` / ``scripts``）。
            job_id: 当前生成任务 id（用于向前端播报阶段）。

        Returns:
            修正后的内容包（原地更新 ``scripts``）。
        """
        slides = list(obj.get("slides") or [])
        scripts = list(obj.get("scripts") or [])
        if not slides or not scripts:
            return obj
        by_id: dict[str, dict[str, Any]] = {
            str(s.get("slide_id")): dict(s) for s in scripts if s.get("slide_id")
        }
        bad = [
            sl for sl in slides
            if self._script_mirrors_slide(sl, str((by_id.get(sl["id"]) or {}).get("text") or ""))
        ]
        if not bad:
            return obj

        logger.info("讲稿与课件雷同，触发重写", extra={"extra_fields": {"slides": len(bad)}})
        self._set_stage(job_id, "讲稿与课件过于雷同，正在重写讲解")
        bad_ids = {sl["id"] for sl in bad}
        payload = {
            "slides": [
                {"id": sl["id"], "title": sl.get("title"), "bullets": sl.get("bullets")}
                for sl in bad
            ],
            "scripts": [
                {"slide_id": sl["id"], "text": (by_id.get(sl["id"]) or {}).get("text") or ""}
                for sl in bad
            ],
        }
        prompt = _SCRIPT_REWRITE_PROMPT.replace(
            "__PAYLOAD__", json.dumps(payload, ensure_ascii=False)[:4000]
        )
        try:
            raw = self._chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "请重写这些页的讲师讲稿。"},
                ],
                max_tokens=2048,
            )
            parsed = self._safe_json(raw) or {}
            fixed = self._clean_scripts(parsed.get("scripts"), bad_ids, bad)
            for item in fixed:
                sid = str(item.get("slide_id") or "")
                cand = str(item.get("text") or "").strip()
                slide = next((s for s in bad if s["id"] == sid), None)
                # 只接受「确实不再雷同」的重写；否则留给下面的确定性扩写兜底。
                if slide and cand and not self._script_mirrors_slide(slide, cand):
                    by_id[sid] = {"slide_id": sid, "text": cand, "cue": "rewritten"}
        except Exception as exc:  # noqa: BLE001 - 重写失败不影响主流程
            logger.warning(
                "讲稿重写失败，将走确定性扩写",
                extra={"extra_fields": {"type": type(exc).__name__}},
            )

        expanded = 0
        for sl in bad:
            cur = str((by_id.get(sl["id"]) or {}).get("text") or "")
            if self._script_mirrors_slide(sl, cur):
                by_id[sl["id"]] = {
                    "slide_id": sl["id"], "text": self._expand_script(sl), "cue": "expanded",
                }
                expanded += 1
        if expanded:
            logger.info("讲稿仍有 %d 页雷同，已确定性扩写", expanded)

        obj["scripts"] = [by_id[s["id"]] for s in slides if s["id"] in by_id]
        return obj

    @staticmethod
    def _norm_title(title: str) -> str:
        """课件页标题归一化：去空白与常见标点、转小写，用于跨讲重复判定。"""
        if not title:
            return ""
        s = str(title)
        for ch in " \t\n\r，。、；：,.;:！？!?“”‘’\"'（）()【】[]《》<>·…—-_/\\|｜~`*+=":
            s = s.replace(ch, "")
        return s.lower().strip()

    @staticmethod
    def _title_dup(a: str, b: str) -> bool:
        """跨讲重复判定（可单测，不再藏在闭包里）。

        规则（保守取向：宁可漏判也不要误判）：
        - 任一为空 → False；
        - 归一化后相等 → True（精确命中）；
        - 否则短标题（≤3 字）**只认精确相等**，防止「极限⊂极限值」「概念⊂概念论」误伤；
        - 长标题允许包含关系，但短者占长者必须 **>0.8** 才算命中。
        """
        na, nb = CourseService._norm_title(a), CourseService._norm_title(b)
        if not na or not nb:
            return False
        if na == nb:
            return True
        shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
        if len(shorter) <= 3:
            return False
        return shorter in longer and (len(shorter) / len(longer)) > 0.8

    _DEDUP_KINDS = (
        "concept", "example", "formula", "quote", "note",
        "diagram", "chart", "table", "takeaway",
    )

    def _dedup_lecture(
        self, course_id: str, lesson_id: str, obj: dict[str, Any], set_stage
    ) -> dict[str, Any]:
        """跨讲去重护栏：本讲课件页标题与同课程其他已落库讲次重复时，定向重写命中的页。

        **非阻塞**：调用失败 / 超时 / JSON 非法 / 重写后仍命中，一律静默跳过或仅 set_stage
        提示，绝不阻断落库主流程。

        Args:
            course_id: 课程 id（去重范围为同课程其他讲次）。
            lesson_id: 当前讲次 id（自身不计入 sibling）。
            obj: 已通过结构校验、即将落库的课堂内容包（含 ``slides``）。
            set_stage: 阶段播报回调（异常时静默吞掉）。
        """
        try:
            slides = list(obj.get("slides") or [])
            if not slides:
                return obj

            # 1) 收集「本讲之前」已落库讲次的课件标题（比对范围 = 已讲过的内容）。
            #    用 global_ordinal < 当前讲次：跨单元也管（用户看到的重复就是重复），
            #    但不许拿**后续**讲次约束本讲（否则重新生成较早讲次会把合法同名页误改）。
            try:
                rows = get_db().query_all(
                    "SELECT slides_json FROM course_lessons "
                    "WHERE course_id=? AND id!=? AND slides_json IS NOT NULL AND status!='pending' "
                    "AND global_ordinal < (SELECT global_ordinal FROM course_lessons WHERE id=?)",
                    (course_id, lesson_id, lesson_id),
                )
                rows = [dict(r) for r in rows]   # sqlite3.Row 无 .get，转 dict 统一取值
            except Exception:
                return obj
            sib_titles: list[str] = []
            for r in rows:
                raw = r.get("slides_json")
                if not raw:
                    continue
                try:
                    sib = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(sib, list):
                    continue
                for sl in sib:
                    if isinstance(sl, dict):
                        t = self._norm_title(sl.get("title"))
                        if t:
                            sib_titles.append(t)
            if not sib_titles:
                return obj

            # 2) 判定命中：交给可单测的 _title_dup（短标题只精确、长标题占比 >0.8）。
            def _hit(t: str) -> bool:
                if not t:
                    return False
                return any(CourseService._title_dup(t, st) for st in sib_titles)

            bad = [sl for sl in slides if _hit(self._norm_title(sl.get("title")))]
            if not bad:
                return obj

            logger.info(
                "跨讲去重：本讲 %d 页标题命中其他讲次", len(bad),
                extra={"extra_fields": {"lesson": lesson_id}},
            )
            try:
                set_stage("检测到与之前讲次重复的课件页，正在改写为本讲内容")
            except Exception:
                pass

            payload = {
                "slides": [
                    {"id": sl["id"], "title": sl.get("title"), "bullets": sl.get("bullets")}
                    for sl in bad
                ]
            }
            prompt = _DEDUP_REWRITE_PROMPT.replace(
                "__PAYLOAD__", json.dumps(payload, ensure_ascii=False)[:4000]
            )
            try:
                raw = self._chat(
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": "请改写这些重复的课件页，只输出 JSON。"},
                    ],
                    max_tokens=4096,
                )
            except Exception as exc:  # noqa: BLE001 - 重写失败不阻断
                logger.warning(
                    "跨讲去重重写调用失败，跳过",
                    extra={"extra_fields": {"type": type(exc).__name__}},
                )
                return obj

            parsed = self._safe_json(raw) or {}
            fixed = parsed.get("slides") if isinstance(parsed, dict) else None
            if not isinstance(fixed, list) or not fixed:
                return obj
            by_fixed: dict[str, dict[str, Any]] = {}
            for it in fixed:
                if not isinstance(it, dict):
                    continue
                sid = str(it.get("id") or "")
                title = str(it.get("title") or "").strip()
                # 字段校验：只要求 title 非空（kind 已不再用于改写，见下方替换逻辑）。
                if not title:
                    continue
                by_fixed[sid] = it

            replaced = 0
            for sl in slides:
                sid = sl.get("id")
                if sid not in by_fixed:
                    continue
                new = by_fixed[sid]
                # 只改内容、不改页型：重写给的 kind 一律忽略（避免借改名突破
                # 「可视化页 ≤2 / takeaway ≤1」等上限，或塞入无 IR 的坏图页）。
                sl["title"] = new.get("title")
                # bullets：裁到 5 条、每条 ≤25 字（与提示词约束一致）。
                raw_b = new.get("bullets") or []
                clean_b: list[str] = []
                for b in raw_b[:5]:
                    b = str(b).strip()
                    if len(b) > 25:
                        b = b[:25]
                    if b:
                        clean_b.append(b)
                if clean_b:
                    sl["bullets"] = clean_b
                if new.get("body"):
                    sl["body"] = new.get("body")
                # citation_refs：只接受本页原有编号集合之内（其余丢弃），
                # 防止重写编造 [[c:N]] 绕过「引用必须来自材料」防伪；
                # 若重写给的全不在集合内，则保留原值。
                orig_refs = set(sl.get("citation_refs") or [])
                new_refs = new.get("citation_refs")
                if isinstance(new_refs, list):
                    kept = [r for r in new_refs if r in orig_refs]
                    sl["citation_refs"] = kept if kept else list(orig_refs)
                replaced += 1

            # 3) 替换完成后重跑一次净化：确保字段自洽、非法图页退化为 note、
            #    并重新受「可视化页 ≤2 / takeaway ≤1」上限约束（去重只改内容、不改页型）。
            obj = self._sanitize_visuals(obj)

            # 4) 重写后仍命中 → 不阻断，只提示。
            still = [sl for sl in (obj.get("slides") or [])
                     if _hit(self._norm_title(sl.get("title")))]
            if still:
                logger.info(
                    "跨讲去重：重写后仍有 %d 页疑似重复，未阻断落库",
                    len(still), extra={"extra_fields": {"lesson": lesson_id}},
                )
                try:
                    set_stage("部分课件页与其他讲次仍可能重复，已尽量改写，请人工复核")
                except Exception:
                    pass
            elif replaced:
                logger.info("跨讲去重：已改写 %d 页", replaced)
            return obj
        except Exception as exc:  # noqa: BLE001 - 整段护栏非阻塞
            logger.warning(
                "跨讲去重护栏异常，跳过",
                extra={"extra_fields": {"type": type(exc).__name__}},
            )
            return obj

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
                # §可视化契约：diagram/chart 原样透传（内容是 spec，不含 [[c:N]]）
                **({"diagram": s["diagram"]} if isinstance(s.get("diagram"), dict) else {}),
                **({"chart": s["chart"]} if isinstance(s.get("chart"), dict) else {}),
                **({"table": s["table"]} if isinstance(s.get("table"), dict) else {}),
                **({"takeaway": s["takeaway"]} if isinstance(s.get("takeaway"), str) else {}),
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
            goal_row = dict(get_db().query_one(
                "SELECT goal FROM courses WHERE id = ?", (lesson["course_id"],)
            ) or {})   # query_one 返回 sqlite3.Row：没有 .get，先转 dict
            unit_title = self._unit_title(lesson["unit_id"])
            goal = (goal_row or {}).get("goal") or ""
            # §1.3 recall：检索 query 扩为 title + objective + 单元标题 + 课程目标
            query = " ".join(x for x in (lesson["title"], lesson["objective"], unit_title, goal) if x)
            hits, context, table = self._material(document_ids, query, top_k=_MAX_HITS)
            if not hits:
                raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")

            self._set_stage(job_id, "正在生成题目")
            hands_mode = self._course_hands_mode(lesson["course_id"])
            allow_hands_on = hands_mode != "off"
            prompt = (
                _PRACTICE_PROMPT
                .replace("__COUNT__", str(count))
                .replace("__DEPTH__", _DEPTH_HINT.get(lesson["depth"], _DEPTH_HINT["standard"]))
                .replace("__TITLE__", lesson["title"])
                .replace("__OBJECTIVE__", lesson["objective"] or lesson["title"])
                .replace("__DESC__", self._desc_block(lesson))
            )
            if hands_mode == "off":
                prompt += ('\n- 本课程**不包含**真实操作类题目：禁止输出 type 为 "hands_on" 的题。\n')
            elif hands_mode == "auto":
                prompt += ('\n- 是否出真实操作类题目（type "hands_on"）**由你按材料判断**：'
                           '材料含可执行的操作/命令/流程/步骤 → 出 1~2 道；'
                           '纯概念叙述材料不要硬造。\n')
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": self._lesson_user(lesson, query, context)},
            ]

            items: list[dict[str, Any]] | None = None
            for _ in range(_MAX_RETRY + 1):
                raw = self._chat(messages, max_tokens=4096)
                parsed = self._safe_json(raw)
                if parsed is not None:
                    validated = self._validate_practice(
                        parsed, count, allow_hands_on=allow_hands_on)
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

            # 跨讲去重护栏：与同课程其他讲次已落库题干比对，命中丢弃并补题（非阻塞）。
            items = self._dedup_practice(
                lesson, items, count, allow_hands_on, context, query, job_id)

            self._persist_practice(lesson_id, items, table)
            get_db().execute(
                "UPDATE course_lessons SET status=CASE WHEN status='done' THEN status"
                " ELSE 'practicing' END, error=NULL, updated_at=? WHERE id=?",
                (now_iso(), lesson_id),
            )
            self._finish_job(job_id)
            # 预生成：只推进「整章」队列（用户显式点的按钮）。
            # ⚠️ 这里**不**自动生成下一讲的讲义 —— 那会变成「生成第 1 讲 → 连锁跑完整门课」，
            #    白白烧掉额度。下一讲的预生成改由用户行为驱动（见 prefetch_lesson）。
            self._pump_prefetch(lesson["course_id"])
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.warning("练习生成失败", extra={"extra_fields": {"type": type(exc).__name__}})
            message = _err_text(exc)
            self._fail_job(job_id, message)
            get_db().execute(
                "UPDATE course_lessons SET error=?, updated_at=? WHERE id=?",
                (message[:500], now_iso(), lesson_id),
            )

    @staticmethod
    def _norm_stem(text: str) -> str:
        """题干/选项归一化（同批次与跨讲去重、选项区分度判定用）。

        字符集与 `_norm_title` 同源，但这里**额外去掉反引号与斜杠** —— 实测模型会
        给出 ``/C:/Users/PC`` 与 ``C:/Users/PC`` 这种「几乎一样」的选项，只去标点抓不到。
        """
        s = str(text or "")
        for ch in " \t\n\r，。、；：,.;:！？!?“”‘’\"'（）()【】[]《》<>·…—-_/\\|｜~`*+=":
            s = s.replace(ch, "")
        return s.lower().strip()

    @staticmethod
    def _validate_practice(obj: dict[str, Any], count: int,
                           allow_hands_on: bool = True) -> list[dict[str, Any]]:
        """校验并归一化题目列表；不合规的题直接丢弃。

        ``allow_hands_on=False``（课程级开关关闭）时，**兜底剥除**模型仍输出的
        hands_on 题——提示词已明确禁止，这里是模型不听话时的第二道防线。
        """
        raw_items = obj.get("items") if isinstance(obj, dict) else obj
        if not isinstance(raw_items, list):
            return []
        out: list[dict[str, Any]] = []
        seen_stems: set[str] = set()
        for it in raw_items[:count]:
            if not isinstance(it, dict):
                continue
            qtype = str(it.get("type") or "single").strip().lower()
            stem = str(it.get("stem") or it.get("question") or "").strip()
            if not stem:
                continue
            # 同批次题干去重：模型偶尔把同一道题写两遍
            # （用户实测「课间答题遇到 2 个一模一样的题目」）。
            stem_key = CourseService._norm_stem(stem)
            if stem_key in seen_stems:
                continue
            seen_stems.add(stem_key)
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
                # 选项必须有实质区别：归一化后出现重复 → 整题丢弃。
                # 实测某题给出 ['/c/Users/PC', 'C:\Users\PC', '/C:/Users/PC', 'C:/Users/PC']，
                # 后两个（连同归一化后的前两个）学员根本分不出，属于废题。
                # 不能只对选项去重 —— 那会让 answer 下标错位。
                if len({CourseService._norm_stem(o) for o in options}) != len(options):
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

            elif qtype == "hands_on":
                # 回填式实操题：题干是真实操作指引，answer 是可接受的回填值数组。
                # 判分完全复用 fill_in（_norm_text 归一化匹配），不新增判分链路。
                if not allow_hands_on:
                    continue
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
                out.append({"type": "hands_on", "stem": stem, "answer": answers,
                            "explanation": explanation})

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

    # ── 预生成（prefetch）：把后续资源提前备好 ─────────────────────
    @staticmethod
    def _prefetch_enabled() -> bool:
        """读预生成开关（默认开）。读不到设置时按默认开处理。"""
        try:
            # ⚠️ `get_settings_service` 在 deps.py 里，不在 settings_service.py ——
            #    早先写错 import 路径 → 每次都落进 except → 开关永远为「开」（被 PF3 断言抓到）。
            from .settings_service import SettingsService

            return SettingsService.get_instance().get_bool("prefetch.enabled")
        except Exception:  # noqa: BLE001 - 设置不可用不该挡住生成链路
            return True

    @staticmethod
    def _course_has_running_job(course_id: str) -> bool:
        """该课程是否已有在跑的任务（保证预生成串行、不撞防重入 1005）。"""
        try:
            rows = get_db().query_all(
                "SELECT id FROM course_jobs WHERE course_id=? AND status='running' LIMIT 1",
                (course_id,),
            )
            return bool(rows)
        except Exception:  # noqa: BLE001
            return True   # 查不到就当作「忙」，宁可不预生成

    @staticmethod
    def _lesson_has_lecture(lesson_id: str) -> bool:
        row = get_db().query_one(
            "SELECT slides_json FROM course_lessons WHERE id=?", (lesson_id,))
        return bool(row and dict(row).get("slides_json"))

    @staticmethod
    def _lesson_has_practice(lesson_id: str) -> bool:
        row = get_db().query_one(
            "SELECT COUNT(*) AS n FROM practice_questions WHERE lesson_id=?", (lesson_id,))
        return bool(row and int(dict(row).get("n") or 0) > 0)

    def _queue_prefetch(self, course_id: str, lesson_id: str, kind: str) -> None:
        """按开关排一个预生成任务：幂等（已有内容就跳过）+ 同课程串行。

        **绝不抛异常**、绝不影响调用它的那条生成链路。``kind`` 取 ``lecture|practice``。
        """
        try:
            if not lesson_id or not self._prefetch_enabled():
                return
            if self._course_has_running_job(course_id):
                # 同课程已有在跑的任务：放弃这次（避免并发打爆限流，也避免撞 1005）。
                logger.info(
                    "预生成跳过：该课程已有在跑的任务",
                    extra={"extra_fields": {"lesson": lesson_id, "kind": kind}},
                )
                return
            if kind == "lecture":
                if self._lesson_has_lecture(lesson_id):
                    return
                self.generate_lecture(lesson_id)
            elif kind == "practice":
                if self._lesson_has_practice(lesson_id):
                    return
                self.generate_practice(lesson_id)
            logger.info(
                "预生成已排队",
                extra={"extra_fields": {"lesson": lesson_id, "kind": kind}},
            )
        except Exception as exc:  # noqa: BLE001 - 预生成永不阻断主流程
            logger.info(
                "预生成放弃：%s", type(exc).__name__,
                extra={"extra_fields": {"lesson": lesson_id, "kind": kind}},
            )

    def prefetch_lesson(self, lesson_id: str) -> dict[str, Any]:
        """**用户驱动**的预生成（只前进一步、不连锁）：本讲缺失的练习 + 下一讲缺失的讲义。

        为什么不让生成链路自己往下推：那会连锁跑完整门课（讲义1→练习1→讲义2→…），
        用户只点了一次却把整门课的额度都花掉。改由「用户打开讲次」这类动作驱动，
        每次只多走一步，用户不前进就不消耗。
        """
        lesson = self._require_lesson(lesson_id)
        cid = str(lesson["course_id"])
        todo: list[tuple[str, str]] = []
        if not self._lesson_has_practice(lesson_id):
            todo.append((lesson_id, "practice"))
        try:
            nxt = get_db().query_one(
                "SELECT id FROM course_lessons WHERE course_id=? AND global_ordinal > ? "
                "AND kind != 'practice' ORDER BY global_ordinal LIMIT 1",
                (cid, lesson["global_ordinal"]),
            )
        except Exception:  # noqa: BLE001
            nxt = None
        if nxt:
            nid = str(dict(nxt)["id"])
            if not self._lesson_has_lecture(nid):
                todo.append((nid, "lecture"))
        if todo:
            self._PREFETCH_QUEUE.setdefault(cid, []).extend(todo)
            self._pump_prefetch(cid)
        return {"queued": len(todo)}

    # ── 整单元预生成（「预生成整章」按钮）──────────────────────────
    # 进程内的待办队列：course_id -> [(lesson_id, kind), ...]。
    # 单机单人，进程内即可；重启丢失只意味着用户再点一次按钮。
    _PREFETCH_QUEUE: dict[str, list[tuple[str, str]]] = {}

    def prefetch_unit(self, uid: str) -> dict[str, Any]:
        """整单元预生成：把该单元所有缺失的「讲义 + 练习」排进队列，逐个串行执行。

        幂等：已有内容的项直接计入 ``skipped``，不会重做。
        返回 ``{queued, skipped, total}`` 供前端核对。
        """
        db = get_db()
        row = db.query_one("SELECT course_id FROM course_units WHERE id = ?", (uid,))
        if row is None:
            raise AppError(1001, "单元不存在")
        course_id = str(dict(row)["course_id"])
        lessons = [
            dict(r) for r in db.query_all(
                "SELECT id, kind FROM course_lessons WHERE unit_id = ? ORDER BY global_ordinal",
                (uid,),
            )
        ]
        todo: list[tuple[str, str]] = []
        skipped = 0
        for les in lessons:
            lid, kind = str(les["id"]), str(les["kind"] or "lecture")
            has_lec = kind == "practice" or self._lesson_has_lecture(lid)
            has_pra = self._lesson_has_practice(lid)
            if not has_lec:
                todo.append((lid, "lecture"))
            if not has_pra:
                todo.append((lid, "practice"))
            if has_lec and has_pra:
                skipped += 1
        if todo:
            self._PREFETCH_QUEUE.setdefault(course_id, []).extend(todo)
            self._pump_prefetch(course_id)
        return {"queued": len(todo), "skipped": skipped, "total": len(lessons)}

    def _pump_prefetch(self, course_id: str) -> bool:
        """推出该课程预生成队列里的下一个任务。

        Returns:
            是否真的排上了一个任务（供自动模式判断要不要兜底）。
        """
        try:
            if not self._prefetch_enabled():
                self._PREFETCH_QUEUE.pop(course_id, None)
                return False
            if self._course_has_running_job(course_id):
                return False     # 等当前任务跑完，它结束时会再推一次
            q = self._PREFETCH_QUEUE.get(course_id) or []
            while q:
                lesson_id, kind = q.pop(0)
                done = (
                    self._lesson_has_lecture(lesson_id) if kind == "lecture"
                    else self._lesson_has_practice(lesson_id)
                )
                if done:
                    continue      # 幂等：期间已被生成过
                self._queue_prefetch(course_id, lesson_id, kind)
                return True
            self._PREFETCH_QUEUE.pop(course_id, None)
            return False
        except Exception as exc:  # noqa: BLE001 - 队列推进永不阻断主流程
            logger.info("预生成队列推进失败：%s", type(exc).__name__)
            return False

    def _dedup_practice(
        self,
        lesson: dict[str, Any],
        items: list[dict[str, Any]],
        count: int,
        allow_hands_on: bool,
        context: str,
        query: str,
        job_id: str,
    ) -> list[dict[str, Any]]:
        """跨讲题目去重：与同课程**本讲之前**已落库讲次的题干比对，命中丢弃；缺口补一次题。

        范围与课件去重护栏（`_dedup_lecture`）口径一致：只跟「已经讲过」的比，
        不拿后续讲次约束本讲。**非阻塞**：任何异常都不影响出题主流程。
        """
        try:
            course_id = lesson["course_id"]
            rows = get_db().query_all(
                "SELECT stem FROM practice_questions WHERE lesson_id IN ("
                " SELECT id FROM course_lessons WHERE course_id=? AND id!=?"
                " AND global_ordinal < (SELECT global_ordinal FROM course_lessons WHERE id=?))",
                (course_id, lesson["id"], lesson["id"]),
            )
            used = {self._norm_stem(dict(r).get("stem")) for r in rows}
            used.discard("")
            if not used:
                return items
            kept = [it for it in items if self._norm_stem(it.get("stem")) not in used]
            dropped = len(items) - len(kept)
            if not dropped:
                return items
            logger.info(
                "练习跨讲去重：丢弃 %d 道与其他讲次重复的题", dropped,
                extra={"extra_fields": {"lesson": lesson["id"]}},
            )
            need = max(0, int(count) - len(kept))
            if need:
                try:
                    self._set_stage(job_id, f"检测到 {dropped} 道题与其他讲次重复，正在补题")
                except Exception:  # noqa: BLE001
                    pass
                kept += self._refill_practice(
                    lesson, need, used, allow_hands_on, context, query)
            return kept or items
        except Exception as exc:  # noqa: BLE001 - 护栏非阻塞
            logger.warning(
                "练习跨讲去重护栏异常，跳过",
                extra={"extra_fields": {"type": type(exc).__name__}},
            )
            return items

    def _refill_practice(
        self,
        lesson: dict[str, Any],
        need: int,
        used: set[str],
        allow_hands_on: bool,
        context: str,
        query: str,
    ) -> list[dict[str, Any]]:
        """补题：把「其他讲次已用过的题干」回灌提示词，要求补 ``need`` 道全新角度的题。"""
        prompt = (
            _PRACTICE_PROMPT
            .replace("__COUNT__", str(need))
            .replace("__DEPTH__", _DEPTH_HINT.get(lesson["depth"], _DEPTH_HINT["standard"]))
            .replace("__TITLE__", lesson["title"])
            .replace("__OBJECTIVE__", lesson["objective"] or lesson["title"])
            .replace("__DESC__", self._desc_block(lesson))
        )
        if not allow_hands_on:
            prompt += ('\n- 本课程**不包含**真实操作类题目：禁止输出 type 为 "hands_on" 的题。\n')
        prompt += (
            "\n\n【本课程其他讲次已经出过的题干（禁止重复，也禁止换皮重复）】\n"
            + "、".join(sorted(used)[:30])
            + f"\n请只输出 {need} 道**全新角度**的题。"
        )
        raw = self._chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": self._lesson_user(lesson, query, context)},
            ],
            max_tokens=2048,
        )
        parsed = self._safe_json(raw)
        if not isinstance(parsed, dict):
            return []
        more = self._validate_practice(parsed, need, allow_hands_on=allow_hands_on)
        return [m for m in more if self._norm_stem(m.get("stem")) not in used]

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

                if it["type"] in ("fill_in", "hands_on"):
                    # 实操题与填空题共用「可接受答案数组」的落库形式；
                    # 漏掉 hands_on 会落到下面的 else 被 str(list) 成字符串，
                    # 判分时按字符遍历 → 必然判错（实测踩到）。
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

    # ── 单题即时判定（答题即时反馈用）────────────────────
    def check_answer(
        self, lesson_id: str, question_id: str, answer: Any
    ) -> dict[str, Any]:
        """判定单题作答并返回解析，**不写入任何记录**。

        用于「每答完一题立刻显示对错 + 解析」：先把判定结果给前端展示，
        而作答记录、错题本与讲次状态仍由最终的 :meth:`grade` 统一落库，
        避免两条写入路径产生不一致（也避免重做时重复计次）。

        题目本身不携带正确答案（见 :meth:`list_questions`），
        正确答案与解析只在本方法返回时下发，**答完才可见**。

        Args:
            lesson_id: 讲次 id（校验题目归属，防止跨讲次提交）。
            question_id: 题目 id。
            answer: 学生作答（选项下标 / 文本）。

        Returns:
            ``{question_id, type, correct, score, feedback, expected, explanation}``。

        Raises:
            AppError: ``1001`` 题目不存在或不属于该讲次。
        """
        row = get_db().query_one(
            "SELECT * FROM practice_questions WHERE id = ? AND lesson_id = ?",
            (question_id, lesson_id),
        )
        if row is None:
            raise AppError(1001, "题目不存在", f"question_id={question_id}")
        res = self._grade_one(row, answer)
        # 选择题额外回传正确选项下标，前端据此高亮正确项（不回传则无法标出）。
        expected_index: int | None = None
        if row["type"] in ("single", "boolean"):
            raw_index = _json_loads(row["answer"], 0)
            try:
                expected_index = int(raw_index)
            except (TypeError, ValueError):
                expected_index = None
        return {
            "question_id": question_id,
            "type": row["type"],
            "correct": bool(res["correct"]),
            "score": float(res["score"]),
            "feedback": res["feedback"],
            "expected": res["expected"],
            "expected_index": expected_index,
            "explanation": row["explanation"] or "",
        }

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

        if qtype in ("fill_in", "hands_on"):
            # 实操题与填空题判分规则一致：可接受答案数组 + 归一化匹配。
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
            # 判分失败时**不能默认判对**（2026-09-19 修）：错题本只在 not correct 时收录，
            # 判对等于把「没判过的题」从薄弱点里抹掉 —— 学生答错也永远看不到这道题。
            # 也不能直接算错（可能本答对了）：给部分分 + 明确写「未判分待核对」，
            # 让它照常进错题本由人复核。
            score, correct = 0.6, False
            feedback = "自动判分未成功（已按部分分 0.6 计），这道题未判对错，请对照参考作答自行核对。"
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
        """创建任务记录，返回 ``job_id``。

        并发防重入（P0-4）：按 ``(course_id, kind, lesson_id)`` 粒度隔离。
        - 讲义 / 练习是**讲次级**（lesson_id 非空，不同讲次可并行）；
        - 大纲 / desc / 单元总结是**课程级**（lesson_id 为 NULL），同课同 kind 互斥。
        - **僵尸豁免**：running 任务 ``updated_at`` 早于 ``_JOB_REENTRY_STALE_MIN`` 分钟
          前视为僵死（进程被杀遗留），不计入防重入。
        """
        db = get_db()
        # NULL 安全比较：COALESCE(lesson_id,'')=COALESCE(?,'')
        running = db.query_one(
            "SELECT id, updated_at FROM course_jobs"
            " WHERE course_id = ? AND kind = ? AND status = 'running'"
            " AND COALESCE(lesson_id,'') = COALESCE(?,'')"
            " ORDER BY updated_at DESC LIMIT 1",
            (course_id, kind, lesson_id),
        )
        if running is not None:
            try:
                updated = parse_iso(str(running["updated_at"]))
                age_min = (datetime.now(timezone.utc) - updated).total_seconds() / 60.0
            except Exception:  # noqa: BLE001 - 时间解析失败按「最新鲜」处理，宁可挡
                age_min = 0.0
            if age_min < _JOB_REENTRY_STALE_MIN:
                raise AppError(1005, "上一个任务还在进行", "请等它完成后再试")
        jid = new_id()
        ts = now_iso()
        db.execute(
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
    def _draft_title(goal: str, document_ids: list[str],
                     prefer_material: bool = False) -> str:
        """创建时的临时标题（默认取目标首句，等模型给出正式标题）。

        ``prefer_material=True`` 用于「目标由课型兜底」的情形：那种句子没有句读，
        取首句会截出「按『XX型』的方…」这种半截串，不如直接用材料名。
        """
        if prefer_material and document_ids:
            row = get_db().query_one(
                "SELECT title FROM documents WHERE id = ?", (document_ids[0],)
            )
            if row:
                return f"《{row['title']}》课程"
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
    def _desc_block(lesson: dict[str, Any]) -> str:
        """讲次教学设计 desc → 提示词块（逐讲生成的稳定性锚点）。

        旧课程 ``desc_json`` 为 NULL：返回空串，提示词与旧版逐字一致（向后兼容）。
        """
        raw = lesson.get("desc_json")
        if not raw:
            return ""
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001 - 脏数据按无 desc 处理
            return ""
        if not isinstance(d, dict) or not d:
            return ""
        # 第二道防线：按讲次类型只渲染对应字段组——即使库里 desc 被污染
        # （lecture 行混入 exercise 字段，或 align 修复写错 kind），也不会串。
        is_practice = str(lesson.get("kind") or "lecture") == "practice"
        rules: list[str] = []
        if not is_practice and d.get("knowledge_points"):
            rules.append("知识点只讲边界清单内的内容")
        if not is_practice and (d.get("transition") or {}).get("avoid"):
            rules.append("avoid 点名的部分留给相邻讲次")
        if not is_practice and ((d.get("transition") or {}).get("prev")
                               or (d.get("transition") or {}).get("next")):
            rules.append("引向/承接只做一句话衔接，不作为课件内容")
        head = ("【本讲教学设计】（大纲阶段已规划，本讲必须遵守：" + "；".join(rules) + "）"
                if rules else "【本讲教学设计】（大纲阶段已规划）")
        lines = [head]
        if not is_practice:
            if d.get("outcomes"):
                lines.append("学习目标：" + "；".join(d["outcomes"]))
            if d.get("knowledge_points"):
                lines.append("知识点边界：" + "；".join(d["knowledge_points"]))
            if d.get("concepts"):
                lines.append("术语口径（全课程统一译名）：" + "、".join(d["concepts"]))
            if d.get("operations"):
                lines.append("涉及操作：" + "；".join(d["operations"]))
            tr = d.get("transition") or {}
            seg = []
            if tr.get("prev"):
                seg.append("承接——" + tr["prev"])
            if tr.get("next"):
                seg.append("引向——" + tr["next"])
            if tr.get("avoid"):
                seg.append("避免展开——" + tr["avoid"])
            if seg:
                lines.append(
                    "讲间衔接（这三项都是**边界**，不是本讲内容）：\n"
                    + "\n".join(f"- {s}" for s in seg)
                    + "\n  - 承接只在第一页用一句话回扣，不要重述上一讲背景；"
                    "引向只在最后一页一句话预告、**严禁展开**；避免展开是硬禁止，任何页都不得以其为主内容"
                )
            if d.get("visual") and d["visual"] != "无":
                lines.append("可视化提示：" + d["visual"])
        else:
            if d.get("exercise_focus"):
                lines.append("考察点（指向哪几讲的内容）：" + "；".join(d["exercise_focus"]))
            if d.get("expected_mistakes"):
                lines.append("学生易错点（供设计干扰项）：" + "；".join(d["expected_mistakes"]))
            if d.get("exercise_flow"):
                lines.append("题型安排：" + d["exercise_flow"])
        if len(lines) == 1:
            return ""
        return "\n".join(lines) + "\n"

    def _siblings_block(self, course_id: str, unit_id: str, lesson_id: str) -> str:
        """本单元讲次地图：让模型看清相邻讲次的地盘，避免跨讲重复（根因 A）。

        返回形如「【本单元讲次地图】（本单元共 N 讲；**你只讲第 k 讲**）…」的文本；
        单讲单元 / 取不到同单元其他讲次时返回空串（不报错）。
        """
        try:
            rows = get_db().query_all(
                "SELECT id,global_ordinal,kind,title,objective,desc_json "
                "FROM course_lessons WHERE unit_id=? ORDER BY global_ordinal",
                (unit_id,),
            )
            rows = [dict(r) for r in rows]   # sqlite3.Row 无 .get，转 dict 统一取值
        except Exception:  # noqa: BLE001 - 旧数据/缺表一律当无地图
            return ""
        if not rows or len(rows) <= 1:
            return ""
        ordered = sorted(rows, key=lambda r: int(r.get("global_ordinal") or 0))
        idx_of = {r["id"]: i + 1 for i, r in enumerate(ordered)}
        cur_idx = idx_of.get(lesson_id, 0)
        n = len(ordered)
        lines = [f"【本单元讲次地图】（本单元共 {n} 讲；**你只讲第 {cur_idx} 讲**，"
                 f"其余各讲的专属内容不许在本讲展开）"]
        for i, r in enumerate(ordered):
            idx = i + 1
            if idx < cur_idx:
                tag = "（已讲）"
            elif idx == cur_idx:
                tag = "← **本讲**"
            else:
                tag = "（后续）"
            lines.append(f"- 第 {idx} 讲 {tag} {r['title']} ｜ {self._lesson_summary_kp(r)}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _lesson_summary_kp(row: dict[str, Any]) -> str:
        """取讲次摘要：desc.knowledge_points 前 3 条（每条 ≤20 字，；连接）；无则退回 objective/标题。"""
        kp: list[str] = []
        raw = row.get("desc_json")
        if raw:
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    kp = (d.get("knowledge_points") or [])[:3]
            except Exception:  # noqa: BLE001
                pass
        if kp:
            return "；".join(str(s)[:20] for s in kp)
        alt = (row.get("objective") or "").strip() or (row.get("title") or "").strip()
        return alt[:20]

    @staticmethod
    def _lesson_user(lesson: dict[str, Any], query: str, context: str) -> str:
        """讲义/练习共用的用户消息：课程信息 + 学习任务 + 编号材料（§1.3）。

        在用户消息头部注入【课程信息】块：课程目标 / 学习者水平 / 上一讲标题 /
        下一讲标题，弥补讲义生成长期缺失的课程级上下文，避免讲次重复、断裂、难度漂移。
        """
        db = get_db()
        course = dict(db.query_one(
            "SELECT goal, level FROM courses WHERE id = ?", (lesson["course_id"],)
        ) or {})   # query_one 返回 sqlite3.Row：没有 .get，先转 dict
        ordinal = int(lesson.get("global_ordinal") or 0)
        prev = db.query_one(
            "SELECT title FROM course_lessons WHERE course_id = ? AND global_ordinal = ?",
            (lesson["course_id"], ordinal - 1),
        ) if ordinal > 1 else None
        nxt = db.query_one(
            "SELECT title FROM course_lessons WHERE course_id = ? AND global_ordinal = ?",
            (lesson["course_id"], ordinal + 1),
        )
        level = _LEVEL_NAME.get(str((course or {}).get("level") or ""), "")
        lines = ["【课程信息】"]
        if course and course["goal"]:
            lines.append(f"课程目标：{course['goal']}")
        if level:
            lines.append(f"学习者水平：{level}")
        if prev:
            lines.append(f"上一讲：{prev['title']}（讲稿结尾可自然承接，不要重讲它）")
        if nxt:
            lines.append(f"下一讲：{nxt['title']}（不抢它的主题）")
        head = "\n".join(lines) + "\n\n"
        task = f"【学习任务】{query}\n\n" if query else ""
        return f"{head}{task}【学习材料检索结果】\n{context}"

    @staticmethod
    def _chat(messages: list[dict[str, str]], *, max_tokens: int = 4096) -> str:
        """调用对话模型（统一温度）。

        用户画像（新手引导收集）在此统一注入：所有课程域调用都经过这里，
        画像全空时 :func:`with_profile` 原样返回，不改变任何既有行为。
        """
        return LLMClient.get_instance().chat(
            with_profile(messages), temperature=0.4, max_tokens=max_tokens
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
            # 实践环节三态：true=含实操 / false=纯理论 / null=自动（前端显示「自动」）。
            # 旧库可能没有该列（迁移前创建的行）→ 按 true 处理，与旧行为一致。
            "hands_on": self._hands_on_out(row),
            # 课型（主/辅，含名称）：旧课程为 None → 前端不显示课型行
            "intent": _intent_out(self._row_get(row, "intent_json")),
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
            # 大纲阶段生成的「本讲教学设计」（旧课程为 None）；大纲预览界面展示用
            "desc": _json_loads(_row_get(row, "desc_json"), None),
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
