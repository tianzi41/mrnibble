"""「主题模式」第一段：让 AI 先把**材料**写出来，再走既有课程链路。

背景（2026-09-17 用户拍板的两段式）：
用户没有材料、只想「打字说想学什么」时，**不要让下游（大纲/讲义/练习）自由发挥** ——
那会让「引用防伪」这条红线失去依据（没有材料就没有真实的 chunk 页码可回填）。
两段式做法：**先让 AI 生成一份材料并真正入库**（`documents` + 分块 + 嵌入 + FTS 索引），
再基于这份材料建课。下游链路一行不改，「只依据材料」的红线继续生效，
引用页码依旧是服务端按 chunk_id 回填的真页码 —— 这是「AI 写材料」与「AI 直接写课」的本质区别。

设计要点：
- **目录先行**：先让模型出目录（JSON），回给前端**让用户审一眼/改一改**，再逐章写正文。
  这是防"材料跑偏"最便宜的一道闸门。
- **逐章生成 + 增量落盘**：每写完一章就追加进 `generations.content_md` ——
  前端数 `## ` 即得「3/5 章」进度（**不需要给表加 stage 字段**），且中途中断不丢已写章节。
- **任务行复用 `generations` 表**：`type='material'`（该列无 CHECK 约束），
  查询直接复用 `GET /api/generations/{gid}`。
- **落库形态**：`documents.source_type='ai'` + `tags=["AI 生成"]` + `meta={topic,...}`
  ——这些列本来就存在且无约束，**不需要 schema 迁移**。
- 失败口径：目录失败 → 整体失败；**单章失败 → 重试一次后跳过并留占位**（任务仍算成功，
  已生成的章节不浪费）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from typing import Any

from ..db.connection import get_db
from ..errors import AppError
from ..utils.ids import new_id
from ..utils.timeutil import now_iso
from .ingest import IngestService
from .llm import LLMClient, extract_json_object

logger = logging.getLogger(__name__)

__all__ = ["MaterialService", "get_material_service", "CHAPTER_PRESETS", "MATERIAL_TYPE"]


def _err_text(exc: Exception) -> str:
    """异常 → 可读文本。

    ``AppError`` 的 ``message`` 是默认表里的短语（例如 2001 就是「模型调用失败」），
    真正有用的原因在 ``detail``（例如「模型端点返回 HTTP 500」）——把两者都带上，
    否则排查时只能看到一个笼统短语（本轮就吃过这个亏）。
    """
    detail = str(getattr(exc, "detail", "") or "")
    msg = str(getattr(exc, "message", "") or "")
    if detail and msg and detail != msg:
        return f"{msg}：{detail}"
    return detail or msg or type(exc).__name__

# 生成任务在 generations.type 上的取值（该列无 CHECK 约束）。
MATERIAL_TYPE = "material"

# 档位 → 章节数（用户拍板：精简 4 / 标准 6 / 详细 8）。
CHAPTER_PRESETS: dict[str, int] = {"brief": 4, "standard": 6, "detailed": 8}
_MAX_CHAPTERS = 12
_MIN_CHAPTERS = 3

_LEVEL_NAME = {"beginner": "零基础", "intermediate": "有基础", "advanced": "进阶"}
_DEPTH_NAME = {"brief": "精简", "standard": "标准", "detailed": "详细"}

# 结构校验失败后的自动重试次数（单章正文也用它）。
_MAX_RETRY = 1
# 单章正文的合理长度下限：低于它认为模型没写正文（触发重试/占位）。
_MIN_CHAPTER_CHARS = 240
# 单章正文的长度要求（提示词里给模型看的区间）。
_CHAPTER_CHARS = "600~1000"

_OUTLINE_PROMPT = """你是教学材料撰写者。学习者想直接学一个主题，但**没有自己的材料**，
需要你先把「用于教学的材料」写出来（之后系统会基于这份材料自动备课，所以材料必须自足、可检索）。

学习主题：__TOPIC__

请先规划这份材料的**目录**。

输出 JSON（只输出一个 JSON 对象，不要输出任何解释或代码块标记）：
{"title": "材料标题（具体、不要空泛）",
 "chapters": [{"title": "第1章 具体标题", "brief": "本章讲什么，一句话"}]}

要求：
1. 共 **__COUNT__ 章**，按**学习顺序**排列：先概念与原理 → 再用法与例子 → 最后常见误区与综合应用。
2. 每章边界清晰、**互不重叠**；标题要具体（禁止「概述」「其他」「补充」这类空标题）。
3. 面向 __LEVEL__ 的学习者，内容深度 __DEPTH__。
4. 覆盖该主题真正重要的部分；不要为了凑章数拆出空壳章节。"""

_CHAPTER_PROMPT = """你是教学材料撰写者。请写出这份教学材料的**第 __INDEX__ 章**正文。

学习主题：__TOPIC__
材料标题：__TITLE__
完整目录（用于把握上下文与前后边界，**你只写第 __INDEX__ 章**）：
__OUTLINE__

本章标题：__CHAPTER_TITLE__
本章要讲：__CHAPTER_BRIEF__

输出要求（**直接输出 Markdown 正文**，不要 JSON、不要用代码块包裹整篇）：
1. **不要写章标题**（系统会按目录补上 `## 第 __INDEX__ 章 <标题>`），
   直接从 2~4 个 `### ` 小节写起。
2. 长度 __CHARS__ 字，以「讲解 + 例子」为主；面向 __LEVEL__，深度 __DEPTH__。
3. 关键术语**第一次出现时给一句解释**（后续系统会引用这段文字，解释要在位）。
4. 结尾用一行 `**本章要点**` 开头，列 3~5 条要点（每条一句话）。
5. ⚠️ **禁止编造**具体的统计数字、研究文献、作者人名、机构、日期与版本号。
   需要举例时使用通用示例，并明确写「示例」。
6. 不要写「见第 X 章」这类交叉引用（章节会被系统重新组织）。"""

_FOOTER = "> 本材料由 AI 生成，请核对关键事实。"


def _clean_md(text: str) -> str:
    """去掉模型可能包上的 ```markdown 围栏，返回纯 Markdown 正文。"""
    t = (text or "").strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", t, re.S)
    if m:
        t = m.group(1).strip()
    return t


def _safe_filename(title: str) -> str:
    """标题 → 安全文件名（去掉 Windows 非法字符）。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (title or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "AI 生成材料")[:60]


class MaterialService:
    """主题模式·材料生成编排（进程级单例）。"""

    _instance: "MaterialService | None" = None

    @classmethod
    def get_instance(cls) -> "MaterialService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 阶段 1：出目录 ───────────────────────────────────
    def start_outline(self, payload: dict[str, Any]) -> dict[str, Any]:
        """起「出目录」任务，返回 ``{generation_id, status}``。

        Raises:
            AppError: 1000 主题为空 / 2000 模型未配置。
        """
        topic = str(payload.get("topic") or "").strip()
        if not topic:
            raise AppError(1000, "请填写想学的主题", "例如：Python 装饰器 / 宏观经济学入门")
        level = str(payload.get("level") or "beginner")
        depth = str(payload.get("depth") or "standard")
        chapters = self._chapter_count(payload)

        llm = LLMClient.get_instance()
        llm.ensure_configured()

        gid = new_id()
        ts = now_iso()
        params = {
            "topic": topic, "level": level, "depth": depth, "chapters": chapters,
            "step": "outline",
        }
        get_db().execute(
            "INSERT INTO generations(id,type,title,document_ids,params,content_md,"
            "content_json,status,model,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (gid, MATERIAL_TYPE, f"[AI] {topic}", "[]",
             json.dumps(params, ensure_ascii=False), None, None, "running",
             llm._config()["model"], ts, ts),
        )
        threading.Thread(
            target=self._run_outline, args=(gid, params), daemon=True
        ).start()
        return {"generation_id": gid, "status": "running"}

    def _run_outline(self, gid: str, params: dict[str, Any]) -> None:
        """后台线程：主题 → 目录 JSON。

        ⚠️ 整个函数包一层 try：**后台线程里任何未捕获异常都会让任务永远停在 running**
        （前端一直转圈、也看不到错误），比"失败"更糟。所有后台任务都必须兜底。
        """
        try:
            self._outline_once(gid, params)
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.warning(
                "目录生成失败",
                extra={"extra_fields": {"gid": gid, "type": type(exc).__name__}},
            )
            self._fail(gid, _err_text(exc))

    def _outline_once(self, gid: str, params: dict[str, Any]) -> None:
        """目录生成的实际逻辑（异常由 :meth:`_run_outline` 统一兜底）。"""
        llm = LLMClient.get_instance()
        prompt = (
            _OUTLINE_PROMPT
            .replace("__TOPIC__", str(params["topic"]))
            .replace("__COUNT__", str(params["chapters"]))
            .replace("__LEVEL__", _LEVEL_NAME.get(str(params["level"]), "零基础"))
            .replace("__DEPTH__", _DEPTH_NAME.get(str(params["depth"]), "标准"))
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请输出目录 JSON。"},
        ]
        outline = None
        last_err: Exception | None = None
        for attempt in range(_MAX_RETRY + 1):
            raw = ""
            try:
                raw = llm.chat(messages, temperature=0.4, max_tokens=2048)
                outline = self._validate_outline(extract_json_object(raw))
                if outline is not None:
                    break
                last_err = AppError(5000, None, "目录结构不符合要求")
            except Exception as exc:  # noqa: BLE001 - 交给重试
                last_err = exc
                outline = None
            messages.append({"role": "assistant", "content": str(raw)[:800]})
            messages.append({
                "role": "user",
                "content": '上一次输出不符合要求。请只输出一个 JSON 对象：'
                           '{"title":"...","chapters":[{"title":"第1章 ...","brief":"..."}]}',
            })
        if outline is None:
            self._fail(gid, str(getattr(last_err, "message", "") or "目录生成失败"))
            return
        self._finish_outline(gid, outline, llm._config()["model"])

    @staticmethod
    def _validate_outline(obj: Any) -> dict[str, Any] | None:
        """校验并归一化目录；不合规返回 None（触发重试）。"""
        if not isinstance(obj, dict):
            return None
        title = str(obj.get("title") or "").strip()
        raw = obj.get("chapters")
        if not isinstance(raw, list):
            return None
        chapters: list[dict[str, str]] = []
        for it in raw[: _MAX_CHAPTERS + 2]:
            if isinstance(it, str):
                t, brief = it.strip(), ""
            elif isinstance(it, dict):
                t = str(it.get("title") or it.get("name") or "").strip()
                brief = str(it.get("brief") or it.get("desc") or "").strip()
            else:
                continue
            if not t:
                continue
            chapters.append({"title": t, "brief": brief[:120]})
        if len(chapters) < _MIN_CHAPTERS:
            return None
        return {"title": title or "AI 生成材料", "chapters": chapters}

    # ── 阶段 2：逐章写正文 → 落成文档 ─────────────────────
    def start_chapters(self, payload: dict[str, Any]) -> dict[str, Any]:
        """起「写正文」任务（目录可被用户改过），返回 ``{generation_id, status}``。"""
        gid = str(payload.get("generation_id") or "").strip()
        row = get_db().query_one("SELECT * FROM generations WHERE id = ?", (gid,))
        if row is None or row["type"] != MATERIAL_TYPE:
            raise AppError(1001, "任务不存在", "请重新生成材料")
        try:
            params = json.loads(row["params"] or "{}")
        except json.JSONDecodeError:
            params = {}
        topic = str(params.get("topic") or "").strip()
        if not topic:
            raise AppError(1000, "任务缺少主题", "请重新开始")

        # 目录：优先用前端提交的（用户可改），否则用上一阶段的结果
        chapters = self._chapters_in(payload.get("chapters"))
        if not chapters:
            try:
                prev = json.loads(row["content_json"] or "{}")
            except json.JSONDecodeError:
                prev = {}
            chapters = self._chapters_in((prev.get("outline") or {}).get("chapters"))
        if not chapters:
            raise AppError(1000, "目录为空", "请先生成目录")
        chapters = chapters[: _MAX_CHAPTERS]

        llm = LLMClient.get_instance()
        llm.ensure_configured()
        params["step"] = "chapters"
        params["chapters"] = len(chapters)
        get_db().execute(
            "UPDATE generations SET params = ?, status = 'running', error = NULL,"
            " content_md = NULL, updated_at = ? WHERE id = ?",
            (json.dumps(params, ensure_ascii=False), now_iso(), gid),
        )
        threading.Thread(
            target=self._run_chapters,
            args=(gid, params, chapters, str(params.get("outline_title") or "")),
            daemon=True,
        ).start()
        return {"generation_id": gid, "status": "running"}

    @staticmethod
    def _chapters_in(raw: Any) -> list[dict[str, str]]:
        """把前端/上一阶段给的目录归一化成 ``[{title, brief}]``。"""
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for it in raw:
            if isinstance(it, str):
                t, brief = it.strip(), ""
            elif isinstance(it, dict):
                t = str(it.get("title") or "").strip()
                brief = str(it.get("brief") or "").strip()
            else:
                continue
            if t:
                out.append({"title": t, "brief": brief[:120]})
        return out

    def _run_chapters(
        self, gid: str, params: dict[str, Any], chapters: list[dict[str, str]], title: str
    ) -> None:
        """后台线程：逐章生成，**每章写完立刻追加到 content_md**，最后落成 documents。"""
        llm = LLMClient.get_instance()
        topic = str(params["topic"])
        level = _LEVEL_NAME.get(str(params.get("level")), "零基础")
        depth = _DEPTH_NAME.get(str(params.get("depth")), "标准")
        title = title or f"{topic}（AI 生成材料）"
        total = len(chapters)
        outline_text = "\n".join(
            f"{i}. {c['title']}｜{c['brief']}" for i, c in enumerate(chapters, 1)
        )

        head = f"# {title}\n\n{_FOOTER}\n"
        self._set_content(gid, head)
        done = 0
        try:
            for idx, ch in enumerate(chapters, 1):
                body = self._one_chapter(
                    llm, gid, idx, total, topic, title, outline_text, ch, level, depth
                )
                piece = f"\n## 第 {idx} 章 {ch['title']}\n\n{body}\n"
                self._append_content(gid, piece)
                if body and "本章生成失败" not in body:
                    done += 1
                logger.info(
                    "材料章节完成",
                    extra={"extra_fields": {"gid": gid, "index": idx, "total": total}},
                )

            md = (get_db().query_one(
                "SELECT content_md FROM generations WHERE id = ?", (gid,)
            ) or {})["content_md"] or head
            doc_id, skip_reason = self._save_as_document(md, title, params)
            if doc_id is None and not skip_reason:
                raise AppError(5000, None, "材料入库失败")
            self._finish_chapters(gid, params, title, doc_id, skip_reason, done, total)
        except Exception as exc:  # noqa: BLE001 - 后台任务兜底
            logger.warning(
                "材料生成失败", extra={"extra_fields": {"gid": gid, "type": type(exc).__name__}}
            )
            self._fail(gid, _err_text(exc))

    def _one_chapter(
        self, llm: LLMClient, gid: str, idx: int, total: int, topic: str, title: str,
        outline_text: str, ch: dict[str, str], level: str, depth: str,
    ) -> str:
        """生成单章正文；重试一次仍失败则返回占位文本（不整体失败）。"""
        prompt = (
            _CHAPTER_PROMPT
            .replace("__INDEX__", str(idx))
            .replace("__TOPIC__", topic)
            .replace("__TITLE__", title)
            .replace("__OUTLINE__", outline_text)
            .replace("__CHAPTER_TITLE__", ch["title"])
            .replace("__CHAPTER_BRIEF__", ch.get("brief") or "（本章无补充说明）")
            .replace("__CHARS__", _CHAPTER_CHARS)
            .replace("__LEVEL__", level)
            .replace("__DEPTH__", depth)
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"请写第 {idx} 章正文（Markdown）。"},
        ]
        for attempt in range(_MAX_RETRY + 1):
            try:
                raw = llm.chat(messages, temperature=0.5, max_tokens=4096)
                body = _clean_md(str(raw))
                # 防御：模型有时仍会自带章标题 → 去掉。外层按目录统一补标题，
                # 否则材料里会出现两个 `##`（前端靠数 `##` 算「已写 N 章」，会数错）。
                body = re.sub(r"^#{1,2}[ \t]+.*?\n+", "", body, count=1).lstrip()
                if len(body) >= _MIN_CHAPTER_CHARS:
                    return body
                messages.append({"role": "assistant", "content": body[:600]})
                messages.append({
                    "role": "user",
                    "content": f"内容太短了。请把第 {idx} 章写完整（{_CHAPTER_CHARS} 字，"
                               "含 2~4 个小节与「本章要点」）。",
                })
            except Exception as exc:  # noqa: BLE001 - 单章失败不影响整份材料
                logger.info(
                    "材料章节失败将重试",
                    extra={"extra_fields": {"gid": gid, "index": idx,
                                            "type": type(exc).__name__}},
                )
        return f"（本章生成失败，可稍后重新生成材料）"

    # ── 落成 documents + 建索引 ──────────────────────────
    @staticmethod
    def _save_as_document(
        md: str, title: str, params: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """把材料写进资料库：复用 ingest 的落盘/建索引，附上 AI 来源标记。

        Returns:
            ``(doc_id, skip_reason)``：命中重复时返回已存在材料的 id 与提示语。
        """
        data = md.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        db = get_db()
        existing = db.query_one(
            "SELECT id, title FROM documents WHERE file_hash = ?", (digest,)
        )
        if existing is not None:
            return existing["id"], f"内容相同的材料已存在（{existing['title']}），直接使用它"

        svc = IngestService.get_instance()
        doc_id, skip_reason = svc.create_from_bytes(
            f"{_safe_filename(title)}.md", data, collection=None
        )
        if doc_id is None:
            return None, skip_reason

        meta = {
            "source": "ai",
            "topic": params.get("topic"),
            "chapters": params.get("chapters"),
            "level": params.get("level"),
            "depth": params.get("depth"),
        }
        db.execute(
            "UPDATE documents SET source_type = 'ai', tags = ?, meta = ?, updated_at = ?"
            " WHERE id = ?",
            (json.dumps(["AI 生成"], ensure_ascii=False),
             json.dumps(meta, ensure_ascii=False), now_iso(), doc_id),
        )
        svc.parse_document(doc_id)   # 分块 + 嵌入 + FTS 索引（下游检索全靠它）
        return doc_id, None

    # ── 落库辅助 ─────────────────────────────────────────
    @staticmethod
    def _set_content(gid: str, md: str) -> None:
        get_db().execute(
            "UPDATE generations SET content_md = ?, updated_at = ? WHERE id = ?",
            (md, now_iso(), gid),
        )

    @classmethod
    def _append_content(cls, gid: str, piece: str) -> None:
        """增量追加一章（前端据此显示「3/5 章」，中断也不丢已写章节）。"""
        db = get_db()
        row = db.query_one("SELECT content_md FROM generations WHERE id = ?", (gid,))
        cur = (row["content_md"] if row else "") or ""
        cls._set_content(gid, cur + piece)

    def _finish_outline(self, gid: str, outline: dict[str, Any], model: str) -> None:
        db = get_db()
        db.execute(
            "UPDATE generations SET content_json = ?, title = ?, status = 'ready',"
            " model = ?, updated_at = ? WHERE id = ?",
            (json.dumps({"outline": outline, "step": "outline"}, ensure_ascii=False),
             f"[AI] {outline['title']}", model, now_iso(), gid),
        )

    def _finish_chapters(
        self, gid: str, params: dict[str, Any], title: str, doc_id: str | None,
        skip_reason: str | None, done: int, total: int,
    ) -> None:
        db = get_db()
        payload = {
            "step": "chapters", "doc_id": doc_id, "title": title,
            "chapters_done": done, "chapters_total": total,
            "skip_reason": skip_reason,
        }
        db.execute(
            "UPDATE generations SET content_json = ?, title = ?, status = 'ready',"
            " updated_at = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), f"[AI] {title}", now_iso(), gid),
        )

    def _fail(self, gid: str, error: str) -> None:
        db = get_db()
        db.execute(
            "UPDATE generations SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error[:500], now_iso(), gid),
        )

    @staticmethod
    def _chapter_count(payload: dict[str, Any]) -> int:
        """章节数：优先用户显式给的数量，否则按档位映射（精简 4 / 标准 6 / 详细 8）。"""
        raw = payload.get("chapter_count")
        try:
            n = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            depth = str(payload.get("depth") or "standard")
            n = CHAPTER_PRESETS.get(depth, CHAPTER_PRESETS["standard"])
        return max(_MIN_CHAPTERS, min(n, _MAX_CHAPTERS))


def get_material_service() -> MaterialService:
    """返回材料生成服务单例。"""
    return MaterialService.get_instance()
