"""五类学习资料生成（架构文档 §6.8，R-C01~C05）。

速查表 / 笔记 / 思维导图 / 练习题 / 闪卡。

设计要点：
- 异步执行：``POST`` 立即返回 ``generation_id + status=running``，后台线程生成，
  前端轮询 ``GET /api/generations/{id}``；
- 每类一个「提示词 + 结构校验 + 归一化」三元组，校验失败自动重试一次；
- 引用沿用 §9 机制：模型只输出 ``[[c:N]]``，页码由服务端按本轮引用表回填；
- 闪卡额外落库 ``flashcards`` 表（供复习页使用，R-C05）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable

from ..db.connection import get_db
from ..errors import AppError
from ..models.guided import GuidedOutput  # noqa: F401  （保持类型一致）
from ..models.retrieval import Hit
from ..utils.ids import new_id
from ..utils.timeutil import now_iso
from .citations import build_context, resolve_citations
from .llm import LLMClient
from .retrieval import get_retrieval_service

logger = logging.getLogger(__name__)

__all__ = ["GenerationService", "get_generation_service", "GENERATION_TYPES"]

GENERATION_TYPES = ("cheatsheet", "notes", "mindmap", "quiz", "flashcard")

# 注入提示词的材料片段上限（控制 prompt 长度，长文档靠检索而非全文）。
_MAX_HITS = 8
# 结构校验失败后的自动重试次数。
_MAX_RETRY = 1

_LENGTH_HINT = {
    "brief": "尽量精简，只保留最核心的内容",
    "standard": "篇幅适中，兼顾覆盖面与精炼",
    "detailed": "详尽一些，覆盖更多细节与易错点",
}

_BASE_RULES = """通用规则：
1. **只依据下方 [材料N]**，引用一律写 [[c:编号]]（编号取自 [材料N] 的 N）；禁止自己书写页码、章节号或文件名。
2. 材料里没有的内容不要编造；确需补充时以「（材料外补充）」开头。
3. 只输出一个 JSON 对象，不要输出 JSON 以外的任何文字（包括解释与代码块标记）。"""

_PROMPTS: dict[str, str] = {
    "cheatsheet": (
        "你是学霸笔记整理器。请依据材料生成一份**速查表**，__LENGTH__。\n"
        "输出 JSON：{\"items\":[{\"point\":\"要点（一句话）\",\"detail\":\"展开说明，"
        "含公式/条件/易错提醒，可含 [[c:N]]\",\"cite\":[N]}]}，至少 5 条。\n" + _BASE_RULES
    ),
    "notes": (
        "你是学习笔记撰写者。请依据材料生成**学习笔记**，__LENGTH__。\n"
        "输出 JSON：{\"sections\":[{\"heading\":\"核心概念|简记口诀|易错点|章节框架 之一\","
        "\"body\":\"Markdown 正文，可含 [[c:N]]\"}]}，必须**恰好包含这 4 个小节**（heading 原样）。\n"
        + _BASE_RULES
    ),
    "mindmap": (
        "你是知识结构梳理者。请依据材料生成一张**思维导图**，__LENGTH__。\n"
        "输出 JSON：{\"root\":{\"name\":\"中心主题\",\"children\":[{\"name\":\"一级节点\","
        "\"children\":[{\"name\":\"二级节点\"}]}]}}，层级 2~3 层，根节点唯一。\n" + _BASE_RULES
    ),
    "quiz": (
        "你是出题老师。请依据材料出一套**单项选择题**，__LENGTH__。\n"
        "输出 JSON：{\"items\":[{\"stem\":\"题干\",\"options\":[\"A\",\"B\",\"C\",\"D\"],"
        "\"answer_index\":0,\"explanation\":\"解析，可含 [[c:N]]\"}]}，共 __COUNT__ 题，"
        "每题 4 个选项，覆盖不同知识点。\n" + _BASE_RULES
    ),
    "flashcard": (
        "你是闪卡设计者。请依据材料生成**问答式闪卡**，__LENGTH__。\n"
        "输出 JSON：{\"items\":[{\"question\":\"只考一个点的问题\",\"answer\":\"简短答案，"
        "可含 [[c:N]]\"}]}，共 __COUNT__ 张，每张聚焦单个知识点。\n" + _BASE_RULES
    ),
}


def _normalize_items(raw: Any, *keys: str) -> list[dict[str, Any]]:
    """从模型输出里稳妥取出条目列表（容忍 items/cards/questions 等别名）。"""
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for k in ("items", "cards", "questions", "sections", "list", "data"):
            v = raw.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # 单对象也包装成单元素
        return [raw] if raw else []
    return []


class GenerationService:
    """资料生成编排（进程级单例）。"""

    _instance: "GenerationService | None" = None

    @classmethod
    def get_instance(cls) -> "GenerationService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 入口 ────────────────────────────────────────────
    def start(
        self,
        type_: str,
        document_ids: list[str] | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """创建生成任务并异步执行，返回 ``{generation_id, status}``。

        Raises:
            AppError: 1000 类型非法 / 2000 模型未配置 / 1002 无可用材料。
        """
        if type_ not in GENERATION_TYPES:
            raise AppError(1000, None, f"type 必须是 {'/'.join(GENERATION_TYPES)}")
        llm = LLMClient.get_instance()
        llm.ensure_configured()

        db = get_db()
        # 标题取来源文档名（最多 2 个）
        titles = self._titles(document_ids)
        if not titles:
            raise AppError(1002, "没有可用材料", "请先上传并解析完成至少一份文档")
        title = f"{'、'.join(titles[:2])} · {_TYPE_NAME[type_]}"

        gid = new_id()
        ts = now_iso()
        db.execute(
            "INSERT INTO generations(id,type,title,document_ids,params,content_md,"
            "content_json,status,model,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                gid, type_, title, json.dumps(document_ids or [], ensure_ascii=False),
                json.dumps(params, ensure_ascii=False), None, None, "running",
                llm._config()["model"], ts, ts,
            ),
        )
        t = threading.Thread(
            target=self._run, args=(gid, type_, document_ids or [], params), daemon=True
        )
        t.start()
        return {"generation_id": gid, "status": "running"}

    # ── 后台执行 ────────────────────────────────────────
    def _run(self, gid: str, type_: str, document_ids: list[str], params: dict[str, Any]) -> None:
        """后台线程：检索 → 提示词 → LLM → 校验 → 落库。"""
        db = get_db()
        try:
            length = str(params.get("length") or "standard")
            count = int(params.get("count") or (10 if type_ in ("quiz", "flashcard") else 0))
            count = max(1, min(count, 50))

            # 检索：用来源文档的代表性文本作为查询（取每篇前几段拼一句）
            query = self._seed_query(document_ids)
            hits, _, _ = get_retrieval_service().hybrid_search(
                query, document_ids=document_ids or None, top_k=_MAX_HITS
            )
            context, table = build_context(hits)
            if not context:
                raise AppError(1002, "没有可用材料", "来源文档没有可检索的文本内容")

            # 注意：模板里含有 JSON 示例的花括号，绝不能用 str.format()（会把
            # {"items":...} 当成占位符解析而抛 KeyError），统一用显式替换。
            prompt = (
                _PROMPTS[type_]
                .replace("__LENGTH__", _LENGTH_HINT.get(length, _LENGTH_HINT["standard"]))
                .replace("__COUNT__", str(count))
            )
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "【学习材料检索结果】\n" + context},
            ]

            llm = LLMClient.get_instance()
            obj: dict[str, Any] | None = None
            last_err: Exception | None = None
            for attempt in range(_MAX_RETRY + 1):
                raw = llm.chat(messages, temperature=0.3, max_tokens=4096)
                try:
                    from .llm import extract_json_object

                    obj = extract_json_object(raw)
                    validated = self._validate(type_, obj, count)
                    if validated is not None:
                        obj = validated
                        break
                    last_err = AppError(5000, None, "结构校验未通过")
                except Exception as exc:  # JSON 解析失败也重试
                    last_err = exc
                    obj = None
                messages.append({"role": "assistant", "content": raw[:1500]})
                messages.append({
                    "role": "user",
                    "content": "你上一次输出不符合 JSON 结构要求，请严格按 schema 重新输出，"
                               "只输出一个 JSON 对象。",
                })
            if obj is None:
                raise last_err or AppError(5000, "生成失败")

            content_md, content_json = self._render(type_, obj, table, generation_id=gid)
            self._finish(gid, content_md, content_json, model=llm._config()["model"])
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.warning("生成任务失败", extra={"extra_fields": {"type": type(exc).__name__}})
            self._fail(gid, str(getattr(exc, "message", "") or type(exc).__name__))

    # ── 结构校验与归一 ──────────────────────────────────
    def _validate(
        self, type_: str, obj: dict[str, Any], count: int
    ) -> dict[str, Any] | None:
        """按类型校验并归一化输出；不合规返回 ``None``（触发重试）。"""
        if type_ == "cheatsheet":
            items = _normalize_items(obj.get("items") or obj)
            out = []
            for it in items:
                point = str(it.get("point") or it.get("title") or "").strip()
                if not point:
                    continue
                out.append({
                    "point": point,
                    "detail": str(it.get("detail") or it.get("content") or "").strip(),
                    "cite": it.get("cite") or [],
                })
            return {"items": out} if len(out) >= 5 else None

        if type_ == "notes":
            sections = _normalize_items(obj.get("sections") or obj)
            heads = {"核心概念", "简记口诀", "易错点", "章节框架"}
            out = []
            for it in sections:
                heading = str(it.get("heading") or it.get("title") or "").strip()
                body = str(it.get("body") or it.get("content") or "").strip()
                if not heading or not body:
                    continue
                out.append({"heading": heading, "body": body})
            got = {s["heading"] for s in out}
            return {"sections": out} if heads.issubset(got) or len(out) >= 4 else None

        if type_ == "mindmap":
            root = obj.get("root") if isinstance(obj.get("root"), dict) else obj
            tree = self._normalize_tree(root)
            return {"root": tree} if tree.get("name") else None

        if type_ == "quiz":
            items = _normalize_items(obj.get("items") or obj.get("questions") or obj)
            out = []
            for it in items:
                stem = str(it.get("stem") or it.get("question") or "").strip()
                options = [str(o) for o in (it.get("options") or []) if str(o).strip()]
                ans = it.get("answer_index")
                if not stem or len(options) < 2:
                    continue
                try:
                    ans_i = int(ans)
                except (TypeError, ValueError):
                    # 兼容 "A"/"B" 形式
                    letter = str(ans or "").strip().upper()
                    ans_i = ord(letter) - 65 if len(letter) == 1 and letter.isalpha() else 0
                if not (0 <= ans_i < len(options)):
                    ans_i = 0
                out.append({
                    "stem": stem, "options": options, "answer_index": ans_i,
                    "explanation": str(it.get("explanation") or it.get("analysis") or "").strip(),
                })
            return {"items": out} if out else None

        if type_ == "flashcard":
            items = _normalize_items(obj.get("items") or obj.get("cards") or obj)
            out = []
            for it in items:
                q = str(it.get("question") or it.get("front") or "").strip()
                a = str(it.get("answer") or it.get("back") or "").strip()
                if q and a:
                    out.append({"question": q, "answer": a})
            return {"items": out} if out else None

        return None

    def _normalize_tree(self, node: Any, depth: int = 0) -> dict[str, Any]:
        """把模型输出的树归一化为 ``{name, children}``（根唯一，防环）。"""
        if depth > 6:
            return {}
        if isinstance(node, str):
            return {"name": node.strip(), "children": []}
        if not isinstance(node, dict):
            return {}
        name = str(
            node.get("name") or node.get("title") or node.get("text") or ""
        ).strip()
        children_raw = node.get("children") or node.get("topics") or []
        children: list[dict[str, Any]] = []
        if isinstance(children_raw, list):
            for c in children_raw[:30]:
                sub = self._normalize_tree(c, depth + 1)
                if sub.get("name"):
                    children.append(sub)
        return {"name": name, "children": children}

    # ── 渲染 ────────────────────────────────────────────
    def _render(
        self, type_: str, obj: dict[str, Any], table: dict[int, Hit],
        *, generation_id: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """把校验后的 JSON 渲染为 ``(content_md, content_json)``，并回填引用页码。"""

        def resolve(text: str) -> str:
            out, _, _ = resolve_citations(text or "", table)
            return out

        if type_ == "cheatsheet":
            items = obj["items"]
            for it in items:
                it["detail"] = resolve(it["detail"])
            md = ["# 速查表", ""]
            for i, it in enumerate(items, 1):
                md.append(f"**{i}. {it['point']}**")
                if it["detail"]:
                    md.append("")
                    md.append(it["detail"])
                md.append("")
            return "\n".join(md), {"items": items}

        if type_ == "notes":
            sections = obj["sections"]
            for s in sections:
                s["body"] = resolve(s["body"])
            md = ["# 学习笔记", ""]
            for s in sections:
                md.append(f"## {s['heading']}")
                md.append("")
                md.append(s["body"])
                md.append("")
            return "\n".join(md), {"sections": sections}

        if type_ == "mindmap":
            root = obj["root"]
            md = self._tree_to_markdown(root)
            return md, {"root": root}

        if type_ == "quiz":
            items = obj["items"]
            for it in items:
                it["explanation"] = resolve(it["explanation"])
            md = ["# 练习题", ""]
            for i, it in enumerate(items, 1):
                md.append(f"**{i}. {it['stem']}**")
                md.append("")
                for j, opt in enumerate(it["options"]):
                    md.append(f"- {chr(65 + j)}. {opt}")
                md.append("")
                md.append(f"> 答案：{chr(65 + it['answer_index'])}")
                if it["explanation"]:
                    md.append(f"> 解析：{it['explanation']}")
                md.append("")
            return "\n".join(md), {"items": items}

        if type_ == "flashcard":
            items = obj["items"]
            for it in items:
                it["question"] = resolve(it["question"])
                it["answer"] = resolve(it["answer"])
            self._persist_flashcards(items, generation_id=generation_id)
            md = ["# 闪卡", ""]
            for i, it in enumerate(items, 1):
                md.append(f"**卡 {i}**  ")
                md.append(f"- 正面：{it['question']}")
                md.append(f"- 背面：{it['answer']}")
                md.append("")
            return "\n".join(md), {"items": items}

        return None, obj

    @staticmethod
    def _tree_to_markdown(root: dict[str, Any]) -> str:
        """树 → markmap 可渲染的层级 Markdown。"""
        lines = [f"# {root.get('name', '思维导图')}", ""]

        def walk(children: list[dict[str, Any]], level: int) -> None:
            for c in children:
                lines.append(f"{'  ' * level}- {c.get('name', '')}")
                walk(c.get("children", []), level + 1)

        walk(root.get("children", []), 0)
        return "\n".join(lines)

    @staticmethod
    def _persist_flashcards(
        items: list[dict[str, Any]], *, generation_id: str | None = None
    ) -> None:
        """闪卡落库（R-C05：供复习页使用），并归属到对应生成产物。"""
        from .flashcards import FlashcardService

        svc = FlashcardService.get_instance()
        for it in items:
            svc.add(
                question=it["question"], answer=it["answer"],
                generation_id=generation_id,
            )

    # ── 辅助 ────────────────────────────────────────────
    @staticmethod
    def _titles(document_ids: list[str] | None) -> list[str]:
        """来源文档标题。"""
        db = get_db()
        if not document_ids:
            rows = db.query_all("SELECT title FROM documents WHERE status='ready' LIMIT 2")
        else:
            marks = ",".join("?" for _ in document_ids)
            rows = db.query_all(
                f"SELECT title FROM documents WHERE id IN ({marks})", tuple(document_ids)
            )
        return [r["title"] for r in rows if r["title"]]

    @staticmethod
    def _seed_query(document_ids: list[str]) -> str:
        """用来源文档前几段拼出检索种子查询。"""
        db = get_db()
        if document_ids:
            marks = ",".join("?" for _ in document_ids)
            rows = db.query_all(
                f"SELECT text FROM chunks WHERE document_id IN ({marks})"
                " ORDER BY ordinal LIMIT 6",
                tuple(document_ids),
            )
        else:
            rows = db.query_all(
                "SELECT text FROM chunks ORDER BY ordinal LIMIT 6"
            )
        joined = " ".join((r["text"] or "")[:120] for r in rows)
        return joined[:600] or "知识点"

    def _finish(self, gid: str, content_md: str | None, content_json: dict[str, Any], *, model: str) -> None:
        """标记任务完成。"""
        db = get_db()
        db.execute(
            "UPDATE generations SET content_md = ?, content_json = ?, status = 'ready',"
            " model = ?, updated_at = ? WHERE id = ?",
            (content_md, json.dumps(content_json, ensure_ascii=False), model, now_iso(), gid),
        )

    def _fail(self, gid: str, error: str) -> None:
        """标记任务失败。"""
        db = get_db()
        db.execute(
            "UPDATE generations SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error[:500], now_iso(), gid),
        )

    # ── 查询 ────────────────────────────────────────────
    def get(self, gid: str) -> dict[str, Any] | None:
        """读取生成产物。"""
        db = get_db()
        row = db.query_one("SELECT * FROM generations WHERE id = ?", (gid,))
        return self._out(row) if row else None

    def list(self, type_: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """列出生成产物。"""
        db = get_db()
        if type_:
            rows = db.query_all(
                "SELECT * FROM generations WHERE type = ? ORDER BY created_at DESC LIMIT ?",
                (type_, limit),
            )
        else:
            rows = db.query_all(
                "SELECT * FROM generations ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [self._out(r) for r in rows]

    def delete(self, gid: str) -> bool:
        """删除生成产物（级联闪卡）。"""
        return get_db().execute("DELETE FROM generations WHERE id = ?", (gid,)).rowcount > 0

    @staticmethod
    def _out(row) -> dict[str, Any]:
        """行 → 对外字典。"""
        try:
            doc_ids = json.loads(row["document_ids"] or "[]")
        except json.JSONDecodeError:
            doc_ids = []
        try:
            params = json.loads(row["params"] or "{}")
        except json.JSONDecodeError:
            params = {}
        try:
            cj = json.loads(row["content_json"]) if row["content_json"] else None
        except json.JSONDecodeError:
            cj = None
        return {
            "id": row["id"], "type": row["type"], "title": row["title"],
            "document_ids": doc_ids, "params": params,
            "content_md": row["content_md"], "content_json": cj,
            "collection": row["collection"], "status": row["status"],
            "error": row["error"], "model": row["model"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }


_TYPE_NAME = {
    "cheatsheet": "速查表",
    "notes": "学习笔记",
    "mindmap": "思维导图",
    "quiz": "练习题",
    "flashcard": "闪卡",
}


def sweep_stale_running() -> int:
    """把「上次进程没跑完」的任务标记为 failed（启动时调用一次）。

    为什么需要：生成任务跑在 ``threading.Thread(daemon=True)`` 里，应用被强杀/崩溃时
    线程随之消失，但库里那行还是 ``status='running'`` —— 用户看到「一直在生成中」的
    材料/课程，实际没有任何东西在跑，而且既不会失败也不会完成（2026-09-19 用户实测
    「切页回来看不出到底跑完没」的一类脏状态）。启动清一次，任务重新变得可重试。

    Returns:
        被清扫的行数（generations + course_jobs）。
    """
    db = get_db()
    now = now_iso()
    total = 0
    # (表, 状态列, 错误列, 时间戳列, 给用户看的说明)
    # ⚠️ course_units 没有 updated_at 列（只有 created_at），时间戳列要按表可选，
    # 否则 UPDATE 直接 OperationalError 被 try 吞掉 = 白写。
    plans = (
        ("generations", "status", "error", "updated_at",
         "生成任务中断（应用已重启或被关闭），可重新生成"),
        ("course_jobs", "status", "error", "updated_at",
         "大纲/讲义任务中断（应用已重启或被关闭），可重新生成"),
        # 单元总结用的是另一组列名（summary_status / summary_error）——漏掉它，
        # 「单元总结」就会永远停在「生成中」（对抗核验时发现的同源漏洞）。
        ("course_units", "summary_status", "summary_error", None,
         "单元总结中断（应用已重启或被关闭），可重新生成"),
    )
    for table, col, err_col, ts_col, message in plans:
        try:
            sets = [f"{col} = 'failed'",
                    f"{err_col} = COALESCE(NULLIF({err_col}, ''), ?)"]
            params: list[Any] = [message]
            if ts_col:
                sets.append(f"{ts_col} = ?")
                params.append(now)
            cur = db.execute(
                f"UPDATE {table} SET {', '.join(sets)} WHERE {col} = 'running'",
                tuple(params),
            )
            total += int(getattr(cur, "rowcount", 0) or 0)
        except Exception:  # noqa: BLE001 - 清扫失败绝不能影响启动（老库可能还没这张表）
            logger.warning("启动清扫未完成任务失败：%s", table, exc_info=True)
    if total:
        logger.info("启动清扫：%d 条上次未完成的任务已标记为失败", total)
    return total


def get_generation_service() -> GenerationService:
    """返回生成服务单例。"""
    return GenerationService.get_instance()
