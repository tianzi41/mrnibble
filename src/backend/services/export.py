"""导出服务（架构文档 §6.9，R-D01~D04）。

- ``anki``    → ``genanki`` 生成 ``.apkg``（可被 Anki 正常导入）；
- ``markdown``/``csv`` → 闪卡 / 思维导图 / 其它 Markdown 产物；
- ``image``   → 前端 markmap 渲染出的 PNG/SVG 回传落盘。

所有导出文件写入 ``data/exports/``，接口只回相对路径，前端经
``GET /api/exports/download?file=<relative>`` 下载（路径校验防目录穿越）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from typing import Any

import genanki

from ..config import get_config
from ..db.connection import get_db
from ..errors import AppError
from ..utils.timeutil import now_iso

logger = logging.getLogger(__name__)

__all__ = ["ExportService", "get_export_service"]

# genanki 需要稳定的数字 id（同 deck 重复导入才不重复建卡组）。
_ANKI_MODEL_ID = 1607392319
_ANKI_MODEL_NAME = "ZhiBan Basic (Q/A)"

_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff\-]+")


def _safe_name(text: str, fallback: str = "export") -> str:
    """把标题转成安全的文件名（保留中文与常用字符）。"""
    cleaned = _SAFE_NAME.sub("_", (text or "").strip()).strip("_")
    return (cleaned or fallback)[:60]


class ExportService:
    """导出编排（进程级单例）。"""

    _instance: "ExportService | None" = None

    @classmethod
    def get_instance(cls) -> "ExportService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 主入口 ──────────────────────────────────────────
    def export(self, generation_id: str, fmt: str) -> dict[str, Any]:
        """导出一个生成产物。

        Raises:
            AppError: 1001 产物不存在 / 1000 格式不支持 / 5001 生成失败。
        """
        db = get_db()
        row = db.query_one("SELECT * FROM generations WHERE id = ?", (generation_id,))
        if row is None:
            raise AppError(1001, "生成任务不存在")
        if row["status"] != "ready":
            raise AppError(1002, "生成尚未完成", f"当前 status={row['status']}")

        gtype = row["type"]
        title = _safe_name(row["title"] or gtype)
        stamp = now_iso()[:19].replace(":", "").replace("-", "")
        base = f"{title}-{stamp}"

        if fmt == "anki":
            if gtype not in ("flashcard", "quiz"):
                raise AppError(1000, None, "Anki 导出仅支持闪卡或练习题产物")
            return self._export_anki(row, gtype, base)
        if fmt == "markdown":
            return self._export_markdown(row, base)
        if fmt == "csv":
            return self._export_csv(row, gtype, base)
        raise AppError(1000, None, "format 必须是 anki/markdown/csv")

    # ── Anki ────────────────────────────────────────────
    def _export_anki(self, row, gtype: str, base: str) -> dict[str, Any]:
        """生成 ``.apkg``（R-D01）。"""
        try:
            items = json.loads(row["content_json"] or "{}")
        except json.JSONDecodeError:
            items = {}
        cards = self._cards_from(items, gtype)
        if not cards:
            raise AppError(5001, "没有可导出的卡片")

        model = genanki.Model(
            _ANKI_MODEL_ID,
            _ANKI_MODEL_NAME,
            fields=[{"name": "Question"}, {"name": "Answer"}],
            templates=[{
                "name": "Card 1",
                "qfmt": "{{Question}}",
                "afmt": '{{FrontSide}}<hr id="answer">{{Answer}}',
            }],
        )
        # deck id 由标题哈希派生：同一产物重复导出得到同一 deck，便于 Anki 去重。
        deck_id = int(hashlib.sha1(base.encode("utf-8")).hexdigest()[:10], 16)
        deck = genanki.Deck(deck_id, base)

        for q, a in cards:
            deck.add_note(genanki.Note(model=model, fields=[q, a]))

        path = get_config().exports_dir / f"{base}.apkg"
        path.parent.mkdir(parents=True, exist_ok=True)
        genanki.Package(deck).write_to_file(str(path))
        logger.info("Anki 导出完成", extra={"extra_fields": {"cards": len(cards)}})
        return self._result(path, "application/octet-stream")

    # ── Markdown ────────────────────────────────────────
    def _export_markdown(self, row, base: str) -> dict[str, Any]:
        """导出 Markdown（R-D02 / R-D03）。"""
        content = row["content_md"] or ""
        if not content.strip():
            # 没有 content_md 时从 content_json 兜底渲染闪卡
            try:
                items = json.loads(row["content_json"] or "{}")
            except json.JSONDecodeError:
                items = {}
            cards = self._cards_from(items, row["type"])
            if not cards:
                raise AppError(5001, "没有可导出的内容")
            content = "\n\n".join(f"**{q}**\n\n{a}" for q, a in cards)
        path = get_config().exports_dir / f"{base}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self._result(path, "text/markdown; charset=utf-8")

    # ── CSV ─────────────────────────────────────────────
    def _export_csv(self, row, gtype: str, base: str) -> dict[str, Any]:
        """导出 CSV（R-D02）。"""
        try:
            items = json.loads(row["content_json"] or "{}")
        except json.JSONDecodeError:
            items = {}

        buf = io.StringIO()
        writer = csv.writer(buf)
        if gtype == "quiz":
            writer.writerow(["stem", "options", "answer_index", "answer", "explanation"])
            for it in items.get("items", []):
                opts = it.get("options") or []
                idx = int(it.get("answer_index") or 0)
                writer.writerow([
                    it.get("stem", ""),
                    " | ".join(str(o) for o in opts),
                    idx,
                    chr(65 + idx) if 0 <= idx < len(opts) else "",
                    it.get("explanation", ""),
                ])
        else:
            cards = self._cards_from(items, gtype)
            if not cards:
                raise AppError(5001, "没有可导出的内容")
            writer.writerow(["question", "answer"])
            for q, a in cards:
                writer.writerow([q, a])

        path = get_config().exports_dir / f"{base}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(buf.getvalue(), encoding="utf-8-sig")  # BOM：Excel 直接打开不乱码
        return self._result(path, "text/csv; charset=utf-8")

    # ── 图片 ────────────────────────────────────────────
    def save_image(self, generation_id: str, filename: str, data: bytes) -> dict[str, Any]:
        """保存前端回传的导图渲染结果（PNG/SVG，R-D04）。"""
        row = get_db().query_one(
            "SELECT type FROM generations WHERE id = ?", (generation_id,)
        )
        if row is None:
            raise AppError(1001, "生成任务不存在")
        suffix = ".svg" if filename.lower().endswith(".svg") else ".png"
        stamp = now_iso()[:19].replace(":", "").replace("-", "")
        base = _safe_name(row["type"] or "mindmap")
        path = get_config().exports_dir / f"{base}-{stamp}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self._result(path, "image/png" if suffix == ".png" else "image/svg+xml")

    # ── 下载 ────────────────────────────────────────────
    @staticmethod
    def resolve_download(relative: str):
        """把相对路径解析为 ``data/`` 内的真实文件（防目录穿越）。

        Raises:
            AppError: 1000 非法路径 / 1001 文件不存在。
        """
        from ..errors import AppError
        from ..paths import data_path, data_root

        rel = (relative or "").strip().lstrip("/")
        if ".." in rel or rel.startswith("\\"):
            raise AppError(1000, "非法路径")
        full = data_path(rel)
        # ⚠️ 只查 ".." 与开头的 "\" 是不够的：Windows 上
        # `data_root().joinpath("C:\\Windows\\win.ini")` 会**丢弃 data_root**、直接指向
        # C:\Windows\win.ini（joinpath 遇到盘符绝对路径就换根）——实测能把 data/ 外的
        # 任意可读文件（含 secret.key）发出去。所以再补一次「解析后必须仍在 data/ 内」
        # 的归位校验，与 coursemedia._safe_path 同一套口径。
        base = data_root().resolve()
        try:
            full.resolve().relative_to(base)
        except (OSError, ValueError) as exc:
            # ValueError = 最终路径确实跑到 base 外面；OSError = 符号链接成环 /
            # 权限不足等无法解析的情况 —— 两种都按「非法路径」拒掉，不要放行。
            raise AppError(1000, "非法路径") from exc
        if not full.exists() or not full.is_file():
            raise AppError(1001, "文件不存在")
        return full

    # ── 内部 ────────────────────────────────────────────
    @staticmethod
    def _cards_from(items: dict[str, Any], gtype: str) -> list[tuple[str, str]]:
        """把产物 JSON 转成 ``(question, answer)`` 对。"""
        cards: list[tuple[str, str]] = []
        if gtype == "quiz":
            for it in items.get("items", []):
                opts = it.get("options") or []
                idx = int(it.get("answer_index") or 0)
                letter = chr(65 + idx) if 0 <= idx < len(opts) else "?"
                body = "<br>".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(opts))
                cards.append((
                    str(it.get("stem", "")),
                    f"<b>{letter}</b><br>{body}<br><br>{it.get('explanation', '')}",
                ))
        else:
            for it in items.get("items", []):
                q = str(it.get("question", "")).strip()
                a = str(it.get("answer", "")).strip()
                if q and a:
                    cards.append((q, a))
        return cards

    @staticmethod
    def _result(path, mimetype: str) -> dict[str, Any]:
        """构造导出结果（相对路径 + 下载地址）。"""
        exports_root = get_config().exports_dir
        rel = path.relative_to(exports_root.parent).as_posix()
        return {
            "file_path": rel,
            "file_name": path.name,
            "size_bytes": path.stat().st_size,
            "download_url": f"/api/exports/download?file={rel}",
        }


def get_export_service() -> ExportService:
    """返回导出服务单例。"""
    return ExportService.get_instance()
