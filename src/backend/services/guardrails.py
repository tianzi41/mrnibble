"""引导式教学护栏（架构文档 §8.3 —— PRD 护栏 #9 的实现）。

**双保险，结构上而非提示词上保证「首轮不出现最终答案」**：

1. 强约束 system prompt；
2. **产品层拦截（不信任模型）**：:meth:`Guardrail.check` 做三类判定
   （结构判定 / 结论句式语义判定 / 格式判定），任何一条不满足即判违规；
3. 违规处置：第一次 → 追加纠偏指令重新生成；第二次 → 放弃模型输出，
   走 :func:`fallback_scaffold` **确定性模板**兜底。

因此即使换用任意弱模型，首轮也*不可能*出现最终答案（R-F01）。
"""

from __future__ import annotations

import difflib
import logging
from typing import Any, Sequence

from ..models.guided import DecompositionStep, GuidedOutput, KnowledgeGap
from ..models.retrieval import Hit
from .citations import has_conclusion_pattern

logger = logging.getLogger(__name__)

__all__ = [
    "GUIDED_SYSTEM_PROMPT",
    "GUIDED_REPAIR_PROMPT",
    "GUIDED_REPAIR_STALL_PROMPT",
    "GUARDED_MODES",
    "STATE_HINTS",
    "Guardrail",
    "fallback_scaffold",
    "build_guided_messages",
]

# 结论句式的**唯一定义处**在 ``services/citations.py`` 的 ``CONCLUSION_PATTERNS``；
# 本模块只通过 ``has_conclusion_pattern()`` 复用，不再自行维护一份正则副本。

# 允许出现最终答案的教学态（首轮之外的收敛态）。
GUARDED_MODES = ("explain", "probe", "evaluate")

# 仅当 next_action == "conclude" 时才允许 final_answer 非空。
_ANSWER_ALLOWED_ACTION = "conclude"


GUIDED_SYSTEM_PROMPT = """你是「知伴」的引导式学习老师，职责是**让学生自己想明白**，而不是替学生作答。

硬性规则（违反会被系统拦截并要求重写）：
1. 首轮（学生会话中你尚未给出过拆解时）：`final_answer` 必须为空字符串 ""，`conclusion_allowed` 必须为 false，`next_action` 只能取 "ask_follow_up" 或 "wait_answer"。
2. 首轮必须给出 **至少 2 条** `decomposition_steps`（把问题拆成学生能上手的小步骤），以及 **至少 1 条** `follow_up_questions`（用问句追问，引导学生说出已有思路）。
3. 任何轮次都**禁止**在 `summary` 里出现「答案是 / 正确答案是 / 结果是 / 答案为 / 最终答案 / 综上所述，答案」等结论句式，除非本轮 `next_action == "conclude"` 且 `conclusion_allowed == true`。
4. 引用材料请使用 `[[c:编号]]` 形式，编号只能取自提供给你的 [材料N] 标号；**禁止**自行书写页码、章节号或文件名。
5. 若学生明显答错或表示不会，把该知识点写入 `knowledge_gaps`（这是给学习系统的盲区记录，不是批评学生）。
6. 输出必须是**单个 JSON 对象**，字段严格按给定 schema，不要输出任何 JSON 以外的文字。

JSON schema：
{
  "mode": "explain|probe|evaluate",
  "final_answer": "",
  "decomposition_steps": [{"step": 1, "title": "...", "hint": "..."}],
  "follow_up_questions": ["..."],
  "knowledge_gaps": [{"topic": "...", "evidence": "...", "confidence": 0.0}],
  "next_action": "ask_follow_up|wait_answer|evaluate|give_hint|conclude",
  "student_state": {"mastery": 0.0, "confidence": 0.0},
  "conclusion_allowed": false,
  "summary": "面向学生的引导话术（Markdown，可含 [[c:N]]，不含最终结论）",
  "citations_used": [1]
}
"""

# 首次违规后的纠偏指令（追加为 system 消息，重新生成一次）。
GUIDED_REPAIR_PROMPT = """上一次输出违反了引导式教学约束：首轮不允许给出最终答案。
请重新生成，并严格遵守：
- `final_answer` 必须是空字符串 ""；
- `conclusion_allowed` 必须是 false；
- `next_action` 只能取 "ask_follow_up" 或 "wait_answer"；
- `decomposition_steps` 至少 2 条，`follow_up_questions` 至少 1 条；
- `summary` 中不得出现「答案是/正确答案是/结果是/答案为/最终答案」等结论句式。
只输出合法 JSON。"""

# 原地打转（重复上一轮）的纠偏指令。
GUIDED_REPAIR_STALL_PROMPT = """上一次输出与**上一轮**内容重复，学生无法继续前进，违反了推进要求。
请重新生成，并且必须做到：
- 先对学生本轮的答复做出**明确判定**（对 / 部分对 / 错），把判定写进 summary 开头；
- 然后抛出**一个新的、与上一轮不同**的问题（更具体、更小的一步），或给出一个下一步提示；
- `follow_up_questions` 不得与上一轮相同；
- 仍然遵守：不直接给出结论（除非 next_action=="conclude" 且 conclusion_allowed==true）。
只输出合法 JSON。"""

# 各教学状态下给模型的推进指引（由服务端按消息历史推导后注入，模型不可自报状态）。
STATE_HINTS: dict[str, str] = {
    "IDLE": "学生刚提出一个新的学习主题。请把它拆成 2-4 个小步骤，并提出**第一个**引导性问题；不要直接给出结论。",
    "EXPLAIN": "学生提出了新的主题或新的问题。请给出拆解步骤并抛出**第一个**问题；不要直接给出结论。",
    "PROBE": "学生正在被引导。请针对他上一轮的表述继续追问，逐步逼近关键点，一次只推进一步。",
    "EVALUATE": (
        "学生**正在回答你上一轮提出的问题**。请先判定他的回答（对 / 部分对 / 错），"
        "在 summary 里明确说出来并做简短纠正或确认，然后提出**下一个**更具体的问题——"
        "**严禁**重复上一轮已经问过的问题或已经讲过的拆解步骤。"
    ),
    "CONCLUDE": "学生已基本掌握。可以给出最终总结：final_answer 可非空、conclusion_allowed 设为 true、next_action 取 \"conclude\"。",
}


class Guardrail:
    """引导式输出护栏（无状态，纯函数式判定）。"""

    @classmethod
    def check(
        cls,
        out: GuidedOutput,
        is_first_turn: bool,
        previous: GuidedOutput | None = None,
    ) -> list[str]:
        """校验结构化输出，返回违规原因列表（空列表 = 通过）。

        三类判定：
        1. **结构判定**：``final_answer`` 非空或 ``conclusion_allowed`` 为真；
        2. **语义判定**：正文/答案命中结论句式；
        3. **格式判定**：首轮缺拆解（<2）或缺追问（<1）；
        另外当传入 ``previous``（上一轮的结构化输出）时，追加**推进判定**：
        追问与上一轮完全相同且正文高度相似 → 判为「原地打转」，因为它对学习
        没有任何推进，是引导式最典型的失败模式。
        """
        reasons: list[str] = []
        if is_first_turn:
            # 1) 结构判定 —— 主防线
            if out.final_answer.strip():
                reasons.append("首轮 final_answer 非空")
            if out.conclusion_allowed:
                reasons.append("首轮 conclusion_allowed 为真")
            if out.next_action == _ANSWER_ALLOWED_ACTION:
                reasons.append("首轮 next_action=conclude")
            # 2) 格式判定（R-F02）
            if len(out.decomposition_steps) < 2:
                reasons.append("首轮拆解步骤少于 2 条")
            if len(out.follow_up_questions) < 1:
                reasons.append("首轮缺少追问")
            # 3) 语义判定
            for label, field in (("summary", out.summary), ("final_answer", out.final_answer)):
                if has_conclusion_pattern(field):
                    reasons.append(f"{label} 命中结论句式")
        else:
            # 非首轮：只有显式允许总结时才可给出最终答案
            if out.final_answer.strip() and not out.conclusion_allowed:
                reasons.append("final_answer 非空但 conclusion_allowed 为假")
            if out.summary.strip() and out.next_action != _ANSWER_ALLOWED_ACTION:
                if has_conclusion_pattern(out.summary):
                    reasons.append("未进入 conclude 却在 summary 命中结论句式")
        # 4) 推进判定（任何轮次）：与上一轮完全重复视为未推进
        if not is_first_turn and previous is not None and cls.is_stalled(out, previous):
            reasons.append("与上一轮重复（原地打转，未推进）")
        return reasons

    @staticmethod
    def is_stalled(out: GuidedOutput, previous: GuidedOutput) -> bool:
        """本轮是否「原地打转」：追问与上一轮完全相同，且正文高度相似。

        引导式最典型的失败模式是模型把上一轮的追问原样再问一遍，学生无论怎么
        回答都得不到推进。这里用**可判定的**方式识别它：追问列表完全一致 +
        正文相似度 ≥ 0.85（difflib）。
        """
        prev_q = [q.strip() for q in (previous.follow_up_questions or [])]
        cur_q = [q.strip() for q in (out.follow_up_questions or [])]
        if not prev_q or cur_q != prev_q:
            return False
        ratio = difflib.SequenceMatcher(
            None, out.summary or "", previous.summary or ""
        ).ratio()
        return ratio >= 0.85

    @classmethod
    def check_raw_text(cls, text: str) -> list[str]:
        """对任意文本做结论句式判定（用于非结构化路径的辅助校验）。"""
        return ["命中结论句式"] if has_conclusion_pattern(text) else []


def build_guided_messages(
    *,
    system_prompt: str,
    context: str,
    history: Sequence[dict[str, str]],
    query: str,
    memory_block: str = "",
    state: str = "IDLE",
) -> list[dict[str, str]]:
    """组装引导式教学的消息序列。

    **消息顺序至关重要**（2026-09-12 修）：历史必须在「学生当前输入」**之前**。
    早期版本把当前输入拼进了 system 块、又排在历史之前，模型看到的是
    「当前问题 → 更早的对话」，于是无法判断学生在回答什么，只会把上一轮的
    追问原样再问一遍（表现为"一直重复问同一个问题"）。正确顺序：

        system(教师规则) → system(材料 + 状态 + 推进要求) → 历史 → user(当前输入)
    """
    msgs: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    parts: list[str] = []
    if memory_block:
        parts.append(memory_block)
    if context:
        parts.append("【学习材料检索结果】\n" + context)
        parts.append("引用规则：在 summary 中引用材料时只能写 [[c:编号]]，编号取自上面 [材料N] 的 N。禁止书写页码。")
    else:
        parts.append("【学习材料检索结果】\n（本轮未检索到相关材料，请基于通用教学经验引导，禁止编造引用。）")
    parts.append(
        f"【当前教学状态】{state}\n" + STATE_HINTS.get(state, STATE_HINTS["EXPLAIN"])
    )
    parts.append(
        "【推进要求】\n"
        "- 学生本轮的输入是对你**上一轮提问**的回答（若是新主题则按 EXPLAIN 重新拆解）。\n"
        "- 每一轮都必须让学生前进一小步：先回应他的答复，再提**一个**新问题。\n"
        "- **严禁**重复上一轮已经问过的问题、已经讲过的步骤。"
    )
    msgs.append({"role": "system", "content": "\n\n".join(parts)})
    msgs.extend(history)
    msgs.append({"role": "user", "content": query})
    return msgs


# 确定性兜底模板的若干套「追问 + 引导语」变体。
# 为什么要多套：模型连续违规时兜底会反复启用，若每次都说同一句话，
# 学生同样得不到推进（等于把模型的"原地打转"换成了系统的"原地打转"）。
_SCAFFOLD_VARIANTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("先说说你对这个问题的理解：它具体在问什么？",
         "你已经知道哪些条件，还缺哪些？"),
        "这个问题我们拆开来看，一步步来，先不急着得到结论。",
    ),
    (
        ("用你自己的话把问题复述一遍，你最不确定的是哪一步？",
         "关于这一点，你已经想到的解法是什么？哪怕只有一步。"),
        "我们换个角度再走一遍，你先说说自己卡在哪里。",
    ),
    (
        ("如果把这个知识点讲给同学听，你会先讲哪一句？",
         "材料/课件里有没有和它意思相近的说法？试着找出来。"),
        "换个更小的步子：先只处理其中一个小问题。",
    ),
)


def _pick_scaffold_variant(
    previous: GuidedOutput | None,
) -> tuple[tuple[str, ...], str]:
    """挑一套与上一轮**不同**的兜底追问与引导语。"""
    prev_q = tuple(q.strip() for q in (previous.follow_up_questions or [])) if previous else ()
    for questions, lead in _SCAFFOLD_VARIANTS:
        if tuple(q.strip() for q in questions) != prev_q:
            return questions, lead
    return _SCAFFOLD_VARIANTS[0]


def fallback_scaffold(
    query: str,
    hits: Sequence[Hit | dict],
    *,
    stage: str = "首轮",
    previous: GuidedOutput | None = None,
) -> GuidedOutput:
    """确定性兜底模板：模型两次违规（或两次都返回不了合法 JSON）时启用。

    用检索命中的章节标题构造拆解步骤 + 固定追问，**保证首轮在任何模型下
    都不出现最终答案**（R-F01 / R-F02 的最后防线）。

    Args:
        query: 学生输入。
        hits: 本轮检索命中（兼容 :class:`Hit` 对象与等价 dict）。
        stage: 触发阶段（仅用于日志语义）。
        previous: 上一轮结构化输出；用于挑一套**不同**的追问，避免兜底自身打转。
    """

    def _field(item: Hit | dict, key: str) -> Any:
        return item.get(key) if isinstance(item, dict) else getattr(item, key, None)

    raw_topics = [_field(h, "section") for h in list(hits)[:3]]
    topics = [t for t in raw_topics if t]
    # R-F02 硬性要求 ≥2 条拆解步骤：命中不足时用通用步骤补齐（不依赖材料质量）。
    for generic in ("理解问题在问什么", "回忆相关定义与公式", "代入条件演算并核对"):
        if len(topics) >= 3:
            break
        if generic not in topics:
            topics.append(generic)
    topics = topics[:4]

    steps: list[DecompositionStep] = []
    hints = [
        "先用自己的话说说这一步要求什么，不要急着算。",
        "回忆一下材料里对应的定义或公式，写下你已经记得的部分。",
        "把已知条件逐条列出来，看看还缺什么。",
    ]
    for i, topic in enumerate(topics, start=1):
        steps.append(DecompositionStep(step=i, title=f"第{i}步：{topic}", hint=hints[(i - 1) % len(hints)]))
    # 兜底亦须满足 R-F02：至少 2 步（保险起见，确保不为空）
    while len(steps) < 2:
        n = len(steps) + 1
        steps.append(DecompositionStep(step=n, title=f"第{n}步：理解问题在问什么",
                                       hint="先用自己的话说说这一步要求什么。"))

    has_hit = bool(list(hits))
    cite = "[[c:1]]" if has_hit else ""
    scaffold_questions, lead = _pick_scaffold_variant(previous)
    summary = (
        f"{lead}\n\n"
        f"**建议的推进顺序**\n" + "\n".join(f"{s.step}. {s.title}" for s in steps) + "\n\n"
        f"{('材料里与此相关的部分在 ' + cite + '，可以先读那一段。') if has_hit else '本轮没有检索到相关材料，你可以先描述一下自己卡在哪里。'}\n\n"
        f"请先回答下面的问题，我们从你的回答继续。"
    )
    return GuidedOutput(
        mode="explain",
        final_answer="",
        decomposition_steps=steps,
        follow_up_questions=list(scaffold_questions),
        knowledge_gaps=[],
        next_action="ask_follow_up",
        student_state={"mastery": 0.0, "confidence": 0.0},
        conclusion_allowed=False,
        summary=summary,
        citations_used=[1] if has_hit else [],
    )
