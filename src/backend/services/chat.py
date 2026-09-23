"""问答编排服务（架构文档 §6.6 / §7.2）。

统一入口 :meth:`ChatService.stream_events`，**流式与非流式共用同一条内部流水线**
（非流式只是消费掉事件流），保证两条路径行为完全一致。

事件序列（SSE）：``meta → delta* → [guided] → citation → done``；异常发 ``error`` 后关闭。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from ..db.connection import get_db
from ..errors import AppError
from ..models.chat import ChatRequest
from ..models.retrieval import Hit
from ..utils.ids import new_id
from ..utils.timeutil import now_iso
from .citations import (
    NOT_MENTIONED_PHRASE,
    build_context,
    resolve_citations,
)
from .embedder import get_embedder
from .events import log_event
from .guided import GuidedService
from .llm import LLMClient
from .profile import with_profile
from .memory import MemoryService
from .retrieval import get_retrieval_service

logger = logging.getLogger(__name__)

__all__ = ["ChatService", "get_chat_service"]

# 注入提示词的材料片段数上限（与检索 top_k 解耦，控制 prompt 长度）。
_MAX_CONTEXT_HITS = 6
# 历史消息条数上限。
_HISTORY_LIMIT = 8

_SYSTEM_NORMAL = """你是「啃书先生」，一位严谨的学习助手。规则：
1. **只依据下方 [材料N] 回答**。引用时只能写 [[c:编号]]（编号取自 [材料N] 的 N），禁止自己书写页码、章节号或文件名。
2. 若材料不足以回答，必须原样输出「材料中未提及」，且不添加任何 [[c:N]]。
3. 材料之外的通用知识一律不得混入；确需补充时必须以「（材料外补充）」开头单独成段。
4. 用 Markdown 回答，条理清晰，中文表述。"""

_LOOSE_SYSTEM = """你是「啃书先生」学习助手。当前开启了「允许材料外回答」：
- 若下方有 [材料N]，优先依据材料回答并用 [[c:编号]] 引用；
- 若没有相关材料，可基于通用知识回答，但必须在回答开头单独一行标注「（材料外回答，未基于当前材料）」；
- 禁止编造页码或把材料外内容伪装成材料内引用。用 Markdown、中文回答。"""


def _no_material_reply(query: str) -> str:
    """材料外问题（严格模式）的确定性回复 —— 结构上杜绝编造引用。"""
    return (
        f"**{NOT_MENTIONED_PHRASE}。**\n\n"
        f"当前资料里没有检索到与「{query[:40]}」直接相关的内容，"
        f"为了不给你编造引用，这里直接如实告知。\n\n"
        f"你可以：\n"
        f"1. 上传相关课件/讲义后再问；\n"
        f"2. 在输入框旁打开「允许材料外回答」，我会用通用知识回答（并明确标注）。\n"
    )


class ChatService:
    """会话与问答编排（进程级单例）。"""

    _instance: "ChatService | None" = None

    @classmethod
    def get_instance(cls) -> "ChatService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 会话 CRUD ───────────────────────────────────────
    def create_conversation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建会话。"""
        db = get_db()
        cid = new_id()
        ts = now_iso()
        title = (payload.get("title") or "新的会话").strip()[:200]
        mode = payload.get("mode") or "normal"
        doc_ids = json.dumps(payload.get("document_ids") or [], ensure_ascii=False)
        db.execute(
            "INSERT INTO conversations(id,title,mode,document_ids,collection,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, title, mode, doc_ids, payload.get("collection"), ts, ts),
        )
        return self.get_conversation(cid) or {}

    def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        """列出会话（按更新时间倒序）。"""
        db = get_db()
        rows = db.query_all(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS n"
            " FROM conversations c ORDER BY c.updated_at DESC LIMIT ?",
            (limit,),
        )
        return [self._conv_out(r) for r in rows]

    def get_conversation(self, cid: str) -> dict[str, Any] | None:
        """读取会话详情（含消息）。"""
        db = get_db()
        row = db.query_one(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS n"
            " FROM conversations c WHERE c.id = ?",
            (cid,),
        )
        if row is None:
            return None
        out = self._conv_out(row)
        msgs = db.query_all(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid",
            (cid,),
        )
        out["messages"] = [self._msg_out(m) for m in msgs]
        return out

    def rename_conversation(self, cid: str, title: str) -> None:
        """重命名会话。"""
        db = get_db()
        if db.query_one("SELECT id FROM conversations WHERE id = ?", (cid,)) is None:
            raise AppError(1001, "会话不存在")
        db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title.strip()[:200], now_iso(), cid),
        )

    def delete_conversation(self, cid: str) -> None:
        """删除会话（级联消息）。"""
        db = get_db()
        cur = db.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        if cur.rowcount == 0:
            raise AppError(1001, "会话不存在")

    # ── 主流水线 ────────────────────────────────────────
    def stream_events(self, req: ChatRequest) -> Iterator[dict[str, Any]]:
        """执行一次问答，逐个产出 SSE 事件字典。

        Yields:
            ``{"event": <type>, "data": <dict>}``。
        """
        try:
            yield from self._pipeline(req)
        except AppError as exc:
            yield {"event": "error", "data": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001 - 兜底，防止 SSE 悬挂
            logger.exception("问答流水线异常")
            yield {"event": "error", "data": {"code": 9000, "message": "服务内部错误"}}

    def run_sync(self, req: ChatRequest) -> dict[str, Any]:
        """非流式执行：消费同一条事件流水线，聚合成完整结果。"""
        result: dict[str, Any] = {
            "conversation_id": req.conversation_id, "message_id": "", "answer": "",
            "citations": [], "grounded": False, "guided": None,
            "retrieved": 0, "memory_used": 0, "model": None,
        }
        for ev in self.stream_events(req):
            kind, data = ev["event"], ev["data"]
            if kind == "meta":
                result["retrieved"] = data.get("retrieved", 0)
                result["memory_used"] = data.get("memory_used", 0)
            elif kind == "delta":
                result["answer"] += data.get("text", "")
            elif kind == "citation":
                result["citations"] = data.get("citations", [])
                result["grounded"] = bool(result["citations"])
                # 流式增量是模型原文（含 [[c:N]]），最终展示/落库文本以服务端回填版为准
                if data.get("content") is not None:
                    result["answer"] = data["content"]
            elif kind == "guided":
                result["guided"] = data
            elif kind == "done":
                result["message_id"] = data.get("message_id", "")
                result["model"] = data.get("model")
                if "grounded" in data:
                    result["grounded"] = data["grounded"]
            elif kind == "error":
                raise AppError(data.get("code", 9000), data.get("message"))
        return result

    # ── 内部 ────────────────────────────────────────────
    def _pipeline(self, req: ChatRequest) -> Iterator[dict[str, Any]]:
        """完整流水线。"""
        db = get_db()
        conv = db.query_one("SELECT * FROM conversations WHERE id = ?", (req.conversation_id,))
        if conv is None:
            raise AppError(1001, "会话不存在")

        llm = LLMClient.get_instance()
        # 半配置（如只填了地址没填模型名）必须说清缺哪一项，否则用户无从下手。
        llm.ensure_configured()

        # 1) 落库用户消息
        history_rows = db.query_all(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid",
            (req.conversation_id,),
        )
        if not history_rows and conv["title"] in (None, "", "新的会话"):
            db.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (req.message[:60], now_iso(), req.conversation_id),
            )
        db.execute(
            "INSERT INTO messages(id, conversation_id, role, content, created_at)"
            " VALUES (?,?,?,?,?)",
            (new_id(), req.conversation_id, "user", req.message, now_iso()),
        )

        # 2) 检索（document_ids：请求 > 会话 > 全库）
        doc_ids = req.document_ids
        if doc_ids is None:
            try:
                doc_ids = json.loads(conv["document_ids"] or "null")
            except json.JSONDecodeError:
                doc_ids = None
        retrieval = get_retrieval_service()
        hits, latency, degraded = retrieval.hybrid_search(
            req.message, document_ids=doc_ids or None
        )
        # 2b) 文档级问题兜底：正文检索为空、但问题指向「材料本身」时，
        #     注入材料概览（标题 + 章节大纲 + 开头片段），避免模型答
        #     「我没有收到文件，请把内容粘贴进来」。材料外问题不受影响。
        outline = ""
        if not hits:
            fallback_hits, outline = retrieval.material_fallback(req.message, doc_ids or None)
            if fallback_hits:
                hits = fallback_hits
                log_event("material_overview", {
                    "conversation_id": req.conversation_id, "chunks": len(hits),
                })
        log_event("retrieval", {
            "conversation_id": req.conversation_id, "hits": len(hits),
            "latency_ms": latency, "degraded": degraded,
        })

        # 3) 记忆召回
        memory_used = 0
        memory_block = ""
        if req.use_memory and get_settings_bool("memory.enabled", True):
            recalled = MemoryService.get_instance().recall(req.message)
            memory_used = len(recalled)
            if recalled:
                lines = [
                    f"- [{m['type']}] {m['content']}" for m in recalled
                ]
                memory_block = "【你记得的关于这位学习者的信息】\n" + "\n".join(lines)

        yield {"event": "meta", "data": {
            "conversation_id": req.conversation_id,
            "retrieved": len(hits),
            "memory_used": memory_used,
            "mode": "guided" if req.guided else "normal",
            "degraded": degraded,
        }}

        # 4) 材料边界（严格模式：无命中 → 确定性回复，绝不调用模型编造）
        #
        # 注意：**引导式会话不走这条短路**。教学是连续对话，学生正在回答问题，
        # 若某一轮恰好没检索到材料就被一句「材料中未提及」掐断，教学线程会断掉
        # （表现为"怎么答都不往下走"）。引导式改为照常进入引导流程——它的提示词
        # 本身已说明「本轮未检索到相关材料，禁止编造引用」，引用仍由结构保证为零。
        grounding = getattr(req, "grounding", "strict")
        if not hits and grounding == "strict" and not req.guided:
            text = _no_material_reply(req.message)
            yield {"event": "delta", "data": {"text": text}}
            yield {"event": "citation", "data": {"citations": []}}
            mid = self._save_message(
                req.conversation_id, "assistant", text, citations=[],
                grounded=False, model=None, content_json=None,
            )
            yield {"event": "done", "data": {"message_id": mid, "grounded": False}}
            return

        # 5) 引导式 / 普通两条分支
        if req.guided:
            yield from self._guided_branch(req, hits, memory_block, history_rows, outline)
        else:
            yield from self._normal_branch(req, hits, memory_block, history_rows, grounding, outline)

    # ── 普通问答分支 ────────────────────────────────────
    def _normal_branch(
        self,
        req: ChatRequest,
        hits: list[Hit],
        memory_block: str,
        history_rows: list,
        grounding: str,
        outline: str = "",
    ) -> Iterator[dict[str, Any]]:
        """普通流式问答。"""
        llm = LLMClient.get_instance()
        context, table = build_context(hits[:_MAX_CONTEXT_HITS], outline=outline)
        messages = self._assemble(
            system=_LOOSE_SYSTEM if grounding == "loose" else _SYSTEM_NORMAL,
            context=context, memory_block=memory_block,
            history=[self._llm_msg(m) for m in history_rows[-_HISTORY_LIMIT:]],
            query=req.message,
        )

        model_name = _current_model_name()
        chunks: list[str] = []
        try:
            for piece in llm.chat_stream(messages):
                chunks.append(piece)
                yield {"event": "delta", "data": {"text": piece}}
        except AppError as exc:
            yield {"event": "error", "data": {"code": exc.code, "message": exc.message}}
            return

        raw = "".join(chunks).strip()
        if not raw:
            raw = "（模型未返回内容，请重试或更换模型端点。）"

        # ── 引用回填（页码唯一来源 = table）─────────────
        content, citations, dropped = resolve_citations(
            raw, table, conversation_id=req.conversation_id
        )
        if dropped:
            logger.info("已丢弃越界引用", extra={"extra_fields": {"count": dropped}})
        # ── 材料边界产品层校验 ─────────────────────────
        grounded = bool(citations)
        if not citations and grounding == "loose" and NOT_MENTIONED_PHRASE not in content \
                and "材料外回答" not in content:
            content = "（材料外回答，未基于当前材料）\n\n" + content

        yield {"event": "citation", "data": {"citations": citations, "content": content}}
        mid = self._save_message(
            req.conversation_id, "assistant", content, citations=citations,
            grounded=grounded, model=model_name, content_json=None,
        )
        yield {"event": "done", "data": {"message_id": mid, "grounded": grounded, "model": model_name}}

    # ── 引导式分支 ──────────────────────────────────────
    def _guided_branch(
        self,
        req: ChatRequest,
        hits: list[Hit],
        memory_block: str,
        history_rows: list,
        outline: str = "",
    ) -> Iterator[dict[str, Any]]:
        """引导式教学（结构化输出 + 护栏）。"""
        turn = GuidedService.get_instance().run(
            conversation_id=req.conversation_id,
            query=req.message,
            messages=[self._msg_dict(m) for m in history_rows],
            hits=hits[:_MAX_CONTEXT_HITS],
            memory_block=memory_block,
            outline=outline,
        )
        payload = turn.output.to_payload()
        payload["citations"] = turn.citations
        payload["source"] = turn.source
        payload["state"] = turn.state
        yield {"event": "guided", "data": payload}
        yield {"event": "delta", "data": {"text": turn.content}}
        yield {"event": "citation", "data": {"citations": turn.citations}}
        mid = self._save_message(
            req.conversation_id, "assistant", turn.content,
            citations=turn.citations, grounded=bool(turn.citations),
            model=_current_model_name(), content_json=payload,
        )
        yield {"event": "done", "data": {"message_id": mid, "grounded": bool(turn.citations)}}

    # ── 存取工具 ────────────────────────────────────────
    def _save_message(
        self,
        cid: str,
        role: str,
        content: str,
        *,
        citations: list[dict[str, Any]],
        grounded: bool,
        model: str | None,
        content_json: dict[str, Any] | None,
    ) -> str:
        """保存 assistant 消息（只存 resolve 后的引用集合）。"""
        db = get_db()
        mid = new_id()
        db.execute(
            "INSERT INTO messages(id, conversation_id, role, content, content_json,"
            " citations, grounded, model, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                mid, cid, role, content,
                json.dumps(content_json, ensure_ascii=False) if content_json else None,
                json.dumps(citations, ensure_ascii=False),
                1 if grounded else 0, model, "ok", now_iso(),
            ),
        )
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), cid)
        )
        return mid

    @staticmethod
    def _assemble(
        *, system: str, context: str, memory_block: str,
        history: list[dict[str, str]], query: str,
    ) -> list[dict[str, str]]:
        """组装普通问答的消息序列。"""
        msgs = [{"role": "system", "content": system}]
        parts = [memory_block] if memory_block else []
        parts.append(
            "【学习材料检索结果】\n" + (context if context else "（本轮未检索到相关材料。）")
        )
        msgs.append({"role": "system", "content": "\n\n".join(parts)})
        msgs.extend(history)
        msgs.append({"role": "user", "content": query})
        # 用户画像（新手引导收集）统一在此注入：所有普通问答消息都经过 _assemble。
        return with_profile(msgs)

    @staticmethod
    def _llm_msg(row) -> dict[str, str]:
        """DB 消息行 → LLM 消息（引用角标保留为 [n] 文本）。"""
        return {"role": row["role"], "content": row["content"] or ""}

    @staticmethod
    def _msg_dict(row) -> dict[str, Any]:
        """DB 消息行 → dict（引导式状态推导用）。"""
        cj = row["content_json"]
        if cj:
            try:
                cj = json.loads(cj)
            except json.JSONDecodeError:
                cj = None
        return {"role": row["role"], "content": row["content"], "content_json": cj}

    @staticmethod
    def _conv_out(row) -> dict[str, Any]:
        """会话行 → 对外字典。"""
        try:
            doc_ids = json.loads(row["document_ids"] or "[]")
        except json.JSONDecodeError:
            doc_ids = []
        return {
            "id": row["id"], "title": row["title"], "mode": row["mode"],
            "document_ids": doc_ids, "collection": row["collection"],
            "message_count": int(row["n"] or 0),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _msg_out(row) -> dict[str, Any]:
        """消息行 → 对外字典。"""
        cj = row["content_json"]
        if cj:
            try:
                cj = json.loads(cj)
            except json.JSONDecodeError:
                cj = None
        try:
            cites = json.loads(row["citations"] or "[]")
        except json.JSONDecodeError:
            cites = []
        return {
            "id": row["id"], "conversation_id": row["conversation_id"],
            "role": row["role"], "content": row["content"], "content_json": cj,
            "citations": cites, "grounded": bool(row["grounded"]),
            "model": row["model"], "created_at": row["created_at"],
        }


def _current_model_name() -> str | None:
    """当前 LLM 模型名（仅用于展示，不含 Key）。"""
    try:
        return get_settings_service_model()
    except Exception:  # pragma: no cover
        return None


def get_settings_service_model() -> str | None:
    """读取模型名。"""
    from ..deps import get_settings_service

    name = get_settings_service().get("llm.model")
    return name or None


def get_settings_bool(key: str, default: bool) -> bool:
    """读取布尔设置（失败回退默认）。"""
    try:
        from ..deps import get_settings_service

        return get_settings_service().get_bool(key)
    except Exception:  # pragma: no cover
        return default


def get_chat_service() -> ChatService:
    """返回问答服务单例。"""
    return ChatService.get_instance()
