"""引导式教学服务（架构文档 §8 —— 本项目最大差异化与风险点）。

职责：
- 组装强约束提示词并调用 LLM 取**结构化**输出；
- 用 :mod:`backend.services.guardrails` 的护栏**校验而非信任**模型输出；
- 违规 → 追加纠偏指令重生成一次 → 仍违规 → :func:`fallback_scaffold` 确定性兜底；
- 识别知识盲区并回写长期记忆（R-F03）；
- 状态迁移写入 ``events``（R-F04 可观测）。

状态机（轻量实现，状态由会话消息历史推导，不额外建表）::

    IDLE --新问题--> EXPLAIN --护栏通过--> PROBE --学生回答--> EVALUATE
    EVALUATE --mastery>=0.8 且 conclude--> CONCLUDE
    EVALUATE --判定盲区/答错--> EXPLAIN（新拆解）
    EVALUATE --部分正确--> PROBE
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from ..models.guided import GuidedOutput
from ..models.retrieval import Hit
from ..utils.timeutil import now_iso
from .citations import build_context, resolve_citations
from .events import log_event
from .guardrails import (
    GUIDED_REPAIR_PROMPT,
    GUIDED_REPAIR_STALL_PROMPT,
    GUIDED_SYSTEM_PROMPT,
    Guardrail,
    build_guided_messages,
    fallback_scaffold,
)
from .llm import LLMClient, extract_json_object
from .profile import with_profile
from .memory import MemoryService

logger = logging.getLogger(__name__)

__all__ = ["GuidedService", "get_guided_service", "GuidedTurn"]

# 会话内最大引导轮数（超过后自动收敛，防止无限追问）。
_MAX_TURNS = 12


class GuidedTurn:
    """一次引导式交互的产出。

    Attributes:
        output: 校验通过的 :class:`GuidedOutput`（可能来自兜底模板）。
        content: 展示文本（Markdown，引用已替换为 ``[n]``）。
        citations: 服务端回填的引用列表。
        source: ``model``（模型输出通过护栏）/ ``model_repaired``（重生成后通过）/
            ``fallback``（确定性兜底）。
        state: 本次交互后的状态机状态。
    """

    __slots__ = ("output", "content", "citations", "source", "state")

    def __init__(
        self,
        output: GuidedOutput,
        content: str,
        citations: list[dict[str, Any]],
        source: str,
        state: str,
    ) -> None:
        self.output = output
        self.content = content
        self.citations = citations
        self.source = source
        self.state = state


class GuidedService:
    """引导式教学编排（进程级单例）。"""

    _instance: "GuidedService | None" = None

    @classmethod
    def get_instance(cls) -> "GuidedService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 状态推导 ────────────────────────────────────────
    @staticmethod
    def last_guided_output(messages: Sequence[dict[str, Any]]) -> GuidedOutput | None:
        """取最近一轮引导式结构化输出（用于推进判定与状态推导）。"""
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content_json"):
                try:
                    return GuidedOutput.model_validate(m["content_json"])
                except Exception:  # noqa: BLE001 - 历史结构异常时忽略
                    return None
        return None

    @staticmethod
    def derive_state(messages: Sequence[dict[str, Any]]) -> str:
        """由会话历史推导当前状态机状态（不额外建表，见 §8.2）。

        **关键**（2026-09-12 修）：只要上一轮**抛出过追问**，学生本轮的输入
        就是在回答它，状态必须是 ``EVALUATE``。早期实现只看上一轮的 ``mode``，
        于是学生答完仍被判为 ``EXPLAIN``，模型被引导去"重新拆解"，表现为
        「一直重复问同一个问题」。
        """
        last_guided = None
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content_json"):
                last_guided = m["content_json"]
                break
        if last_guided is None:
            return "IDLE"
        action = last_guided.get("next_action")
        if action == "conclude" or last_guided.get("conclusion_allowed"):
            return "CONCLUDE"
        # 上一轮问过学生 → 本轮是学生在回答
        if last_guided.get("follow_up_questions"):
            return "EVALUATE"
        if last_guided.get("mode") == "evaluate":
            return "EVALUATE"
        return "PROBE" if last_guided.get("mode") == "probe" else "EXPLAIN"

    @staticmethod
    def is_first_turn(messages: Sequence[dict[str, Any]]) -> bool:
        """首轮判定：**服务端**依据会话内是否已有引导式 assistant 消息。

        绝不信任模型自报的轮次（架构文档 §8.3 第 5 条）。
        """
        return not any(
            m.get("role") == "assistant" and m.get("content_json") for m in messages
        )

    # ── 主流程 ──────────────────────────────────────────
    def run(
        self,
        *,
        conversation_id: str,
        query: str,
        messages: Sequence[dict[str, Any]],
        hits: Sequence[Hit],
        memory_block: str = "",
        outline: str = "",
    ) -> GuidedTurn:
        """执行一轮引导式教学。

        Args:
            conversation_id: 会话 id（用于事件与盲区记忆关联）。
            query: 学生本轮输入。
            messages: 该会话历史（**不含**本轮输入，当前输入会被拼成最后一条 user 消息）。
            hits: 本轮检索命中（引用页码的唯一来源）。
            memory_block: 已格式化的记忆注入文本。
            outline: 可选的「材料概览」（文档级问题兜底时注入，不参与引用编号）。

        Returns:
            :class:`GuidedTurn`。
        """
        llm = LLMClient.get_instance()
        context, table = build_context(hits, outline=outline)
        history = self._history(messages)
        state_before = self.derive_state(messages)
        first_turn = self.is_first_turn(messages)
        previous = self.last_guided_output(messages)
        turn_no = sum(1 for m in messages if m.get("role") == "assistant")

        base_msgs = build_guided_messages(
            system_prompt=GUIDED_SYSTEM_PROMPT,
            context=context,
            history=history,
            query=query,
            memory_block=memory_block,
            state=state_before,
        )
        if turn_no >= _MAX_TURNS:
            base_msgs.append({
                "role": "system",
                "content": "本轮已是第 12 轮，若学生已基本掌握请输出 next_action=\"conclude\""
                           " 与 final_answer 做总结；否则继续追问一次。",
            })

        out: GuidedOutput | None = None
        source = "model"
        stalled = False

        # 第 1 次尝试
        raw = self._attempt(llm, base_msgs)
        if raw is not None:
            violations = Guardrail.check(raw, first_turn, previous)
            if not violations:
                out = raw
            else:
                stalled = any("重复" in v for v in violations)
                log_event("guided_state", {
                    "conversation_id": conversation_id, "event": "violation",
                    "attempt": 1, "state": state_before, "reasons": violations[:4],
                })
                logger.warning("引导式输出违规，重生成", extra={"extra_fields": {"reasons": violations}})
        # 第 2 次尝试（按违规类型给对应纠偏指令）
        if out is None:
            repair = GUIDED_REPAIR_STALL_PROMPT if stalled else GUIDED_REPAIR_PROMPT
            repaired = self._attempt(llm, base_msgs + [{"role": "system", "content": repair}])
            if repaired is not None and not Guardrail.check(repaired, first_turn, previous):
                out = repaired
                source = "model_repaired"
        # 兜底模板（确定性，保证首轮永不给答案；且换一套措辞避免再次重复）
        if out is None:
            out = fallback_scaffold(query, hits, previous=previous)
            source = "fallback"
            logger.warning("引导式输出两次违规，已启用兜底模板",
                           extra={"extra_fields": {"conversation_id": conversation_id}})

        # ── 引用回填（页码唯一来源 = table，即 chunks 表）────
        content, citations, _ = resolve_citations(
            out.summary or "", table, conversation_id=conversation_id
        )
        if not content:
            content = "我们先把这个问题拆开，一步步来。"

        # ── 盲区回写（R-F03）────────────────────────────
        for gap in out.knowledge_gaps:
            self._capture_gap(gap, conversation_id)

        # ── 状态迁移（R-F04）────────────────────────────
        state_after = self._next_state(state_before, out)
        log_event("guided_state", {
            "conversation_id": conversation_id, "event": "transition",
            "from": state_before, "to": state_after, "source": source,
            "first_turn": first_turn, "steps": len(out.decomposition_steps),
            "questions": len(out.follow_up_questions),
        })

        return GuidedTurn(out, content, citations, source, state_after)

    # ── 内部 ────────────────────────────────────────────
    def _attempt(self, llm: LLMClient, msgs: list[dict[str, str]]) -> GuidedOutput | None:
        """调用一次 LLM 并解析为 :class:`GuidedOutput`；失败返回 ``None``。"""
        try:
            obj = llm.chat_json(with_profile(msgs), temperature=0.3)
        except Exception as exc:  # AppError/解析失败统一走兜底
            logger.warning("引导式调用失败", extra={"extra_fields": {"type": type(exc).__name__}})
            return None
        try:
            return GuidedOutput.model_validate(obj)
        except Exception as exc:
            logger.warning("引导式输出 schema 不合规",
                           extra={"extra_fields": {"detail": type(exc).__name__}})
            # schema 失败时尝试宽松提取关键字段
            return self._lenient_parse(obj)

    def _lenient_parse(self, obj: dict[str, Any]) -> GuidedOutput | None:
        """对字段名有偏差的输出做一次宽松归一（仍交由护栏判定）。"""
        if not isinstance(obj, dict):
            return None
        norm = {
            "mode": obj.get("mode") or "explain",
            "final_answer": obj.get("final_answer") or obj.get("answer") or "",
            "decomposition_steps": obj.get("decomposition_steps") or obj.get("steps") or [],
            "follow_up_questions": obj.get("follow_up_questions") or obj.get("questions") or [],
            "knowledge_gaps": obj.get("knowledge_gaps") or obj.get("gaps") or [],
            "next_action": obj.get("next_action") or "ask_follow_up",
            "student_state": obj.get("student_state") or {},
            "conclusion_allowed": bool(obj.get("conclusion_allowed", False)),
            "summary": obj.get("summary") or obj.get("content") or "",
            "citations_used": obj.get("citations_used") or [],
        }
        try:
            return GuidedOutput.model_validate(norm)
        except Exception:
            return None

    @staticmethod
    def _history(messages: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
        """把会话历史压成 LLM 消息（只取最近 6 条，避免超长）。"""
        out: list[dict[str, str]] = []
        for m in messages[-6:]:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = (m.get("content") or "").strip()
            if not content:
                continue
            # 引导式历史中的 assistant 消息截断，避免把上一轮长文重复注入。
            if role == "assistant":
                content = content[:600]
            out.append({"role": role, "content": content})
        return out

    @staticmethod
    def _next_state(current: str, out: GuidedOutput) -> str:
        """按 §8.2 迁移表推进状态。"""
        if out.next_action == "conclude" and out.final_answer.strip():
            return "CONCLUDE"
        if out.mode == "evaluate":
            mastery = float(out.student_state.get("mastery", 0.0))
            if mastery >= 0.8 and out.next_action == "conclude":
                return "CONCLUDE"
            if out.knowledge_gaps:
                return "EXPLAIN"
            return "PROBE"
        if out.mode == "probe":
            return "PROBE"
        return "EXPLAIN"

    @staticmethod
    def _capture_gap(gap, conversation_id: str) -> None:
        """把盲区写入长期记忆（type=knowledge_gap，R-F03）。"""
        try:
            svc = MemoryService.get_instance()
            content = f"知识盲区：{gap.topic}" + (f"（{gap.evidence}）" if gap.evidence else "")
            # 同主题去重：已存在同内容盲区则跳过
            existing, _ = svc.list(type_="knowledge_gap", page=1, page_size=500)
            if any(gap.topic and gap.topic in e["content"] for e in existing):
                return
            svc.add(
                content,
                type_="knowledge_gap",
                source="auto",
                confidence=float(gap.confidence),
                conversation_id=conversation_id,
            )
        except Exception:  # pragma: no cover - 盲区写入失败不阻断教学
            logger.warning("盲区记忆写入失败", exc_info=True)

    # ── 收敛总结（进入 CONCLUDE）───────────────────────
    def conclude(self, *, conversation_id: str, query: str, summary_text: str) -> dict[str, Any]:
        """记录一次「掌握」事件，并把学习进度写入长期记忆。"""
        log_event("guided_state", {
            "conversation_id": conversation_id, "event": "conclude",
        })
        try:
            MemoryService.get_instance().add(
                f"学习进度：{summary_text[:160]}",
                type_="progress",
                source="auto",
                confidence=0.7,
                conversation_id=conversation_id,
            )
        except Exception:  # pragma: no cover
            logger.warning("进度记忆写入失败", exc_info=True)
        return {"conversation_id": conversation_id, "query": query}


def get_guided_service() -> GuidedService:
    """返回引导式服务单例。"""
    return GuidedService.get_instance()
