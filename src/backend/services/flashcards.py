"""闪卡与间隔重复（架构文档 §4.2 ``flashcards`` 表 / 复习页）。

复习算法采用 **SM-2 简化版**：quality ∈ {again(0), hard(3), good(4), easy(5)}，
依据 ease/interval/reps/lapses 更新下次到期时间。答错可联动写入知识盲区（R-F03）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db.connection import get_db
from ..errors import AppError
from ..utils.ids import new_id
from ..utils.timeutil import now_iso
from ..utils.timeutil import parse_iso

logger = logging.getLogger(__name__)

__all__ = ["FlashcardService", "get_flashcard_service", "REVIEW_QUALITY"]

REVIEW_QUALITY = {"again": 0, "hard": 3, "good": 4, "easy": 5}


def _default_state() -> dict[str, Any]:
    """新卡的复习状态。"""
    return {"ease": 2.5, "interval": 0, "reps": 0, "lapses": 0, "due": now_iso()}


class FlashcardService:
    """闪卡 CRUD 与复习状态机（进程级单例）。"""

    _instance: "FlashcardService | None" = None

    @classmethod
    def get_instance(cls) -> "FlashcardService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── CRUD ────────────────────────────────────────────
    def add(
        self,
        *,
        question: str,
        answer: str,
        generation_id: str | None = None,
        document_id: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """新增闪卡。"""
        db = get_db()
        cid = new_id()
        ts = now_iso()
        db.execute(
            "INSERT INTO flashcards(id, generation_id, document_id, question, answer,"
            " tags, sr_state, created_at, updated_at, last_reviewed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                cid, generation_id, document_id, question.strip(), answer.strip(),
                json.dumps(tags or [], ensure_ascii=False),
                json.dumps(_default_state(), ensure_ascii=False), ts, ts, None,
            ),
        )
        return self.get(cid)

    def get(self, cid: str) -> dict[str, Any]:
        """读取单张闪卡。"""
        row = get_db().query_one("SELECT * FROM flashcards WHERE id = ?", (cid,))
        if row is None:
            raise AppError(1001, "闪卡不存在")
        return self._out(row)

    def list(
        self,
        *,
        generation_id: str | None = None,
        due_only: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """列出闪卡；``due_only=True`` 只返回已到期的卡。"""
        db = get_db()
        sql = "SELECT * FROM flashcards"
        where: list[str] = []
        params: list[Any] = []
        if generation_id:
            where.append("generation_id = ?")
            params.append(generation_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = db.query_all(sql, tuple(params))
        items = [self._out(r) for r in rows]
        if due_only:
            now = now_iso()
            items = [i for i in items if (i["sr_state"].get("due") or "") <= now]
        return items

    def delete(self, cid: str) -> bool:
        """删除闪卡。"""
        return get_db().execute("DELETE FROM flashcards WHERE id = ?", (cid,)).rowcount > 0

    # ── 复习（SM-2 简化版）─────────────────────────────
    def review(self, cid: str, result: str) -> dict[str, Any]:
        """记录一次复习并更新到期时间。

        Args:
            cid: 闪卡 id。
            result: ``again|hard|good|easy``（``again`` 也会记为一次 lapse）。

        Raises:
            AppError: 1000 结果非法 / 1001 卡不存在。
        """
        if result not in REVIEW_QUALITY:
            raise AppError(1000, None, "result 必须是 again/hard/good/easy")
        db = get_db()
        row = db.query_one("SELECT * FROM flashcards WHERE id = ?", (cid,))
        if row is None:
            raise AppError(1001, "闪卡不存在")
        try:
            state = json.loads(row["sr_state"] or "{}") or _default_state()
        except json.JSONDecodeError:
            state = _default_state()

        q = REVIEW_QUALITY[result]
        ease = float(state.get("ease", 2.5))
        interval = int(state.get("interval", 0))
        reps = int(state.get("reps", 0))
        lapses = int(state.get("lapses", 0))

        if q < 3:  # again
            reps = 0
            interval = 0
            lapses += 1
            ease = max(1.3, ease - 0.2)
            due_delta = timedelta(minutes=10)
        else:
            ease = max(1.3, ease + (0.1 if q == 5 else 0.0 if q == 4 else -0.15))
            reps += 1
            if reps == 1:
                interval = 1
            elif reps == 2:
                interval = 6
            else:
                interval = max(1, int(round(interval * ease)))
            due_delta = timedelta(days=interval)

        due = (datetime.now(timezone.utc) + due_delta).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        new_state = {"ease": round(ease, 3), "interval": interval, "reps": reps,
                     "lapses": lapses, "due": due}
        db.execute(
            "UPDATE flashcards SET sr_state = ?, last_reviewed_at = ?, updated_at = ?"
            " WHERE id = ?",
            (json.dumps(new_state, ensure_ascii=False), now_iso(), now_iso(), cid),
        )
        return self.get(cid)

    # ── 内部 ────────────────────────────────────────────
    @staticmethod
    def _out(row) -> dict[str, Any]:
        """行 → 对外字典。"""
        try:
            tags = json.loads(row["tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        try:
            state = json.loads(row["sr_state"] or "{}")
        except json.JSONDecodeError:
            state = _default_state()
        return {
            "id": row["id"], "generation_id": row["generation_id"],
            "document_id": row["document_id"], "question": row["question"],
            "answer": row["answer"], "tags": tags, "sr_state": state,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "last_reviewed_at": row["last_reviewed_at"],
        }


def get_flashcard_service() -> FlashcardService:
    """返回闪卡服务单例。"""
    return FlashcardService.get_instance()
