"""课程资料媒体服务（P1：材料原文读取 / 页渲染 / 导出）。

用途：

- :meth:`CourseMediaService.raw_file` —— 取材料原始 PDF，供前端 pdf.js 渲染与标注
  （**默认路径**，零新增依赖）；
- :meth:`CourseMediaService.render_page` —— 服务端把某一页渲染成 PNG（**可选**，
  需要 PyMuPDF）；
- :meth:`CourseMediaService.lesson_markdown` / :meth:`conversation_markdown` —— 导出。

**关于 PyMuPDF（重要）**：PyMuPDF 是 AGPL-3.0 许可，而本项目刻意只用 MIT/BSD
类宽松依赖（见 ``docs/06-第三方依赖与许可.md``）。因此服务端渲染**默认不启用**：
前端用已内置的 pdf.js 完成页面渲染与标注，功能完整、无许可风险；
``/page`` 接口保留给「需要服务端出图」的场景（如导出、缩略图），
未安装 PyMuPDF 时返回**明确错误**，绝不返回假图。

约束（与既有模块一致）：

- 只在**本机**读 ``data/files`` 下的文件，绝不上外传；
- 路径必须落在 ``files_dir`` 内，杜绝目录穿越。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import get_config
from ..db.connection import get_db
from ..errors import AppError

logger = logging.getLogger(__name__)

__all__ = ["CourseMediaService", "get_course_media_service"]

# 渲染缩放（约 150 DPI 起，兼顾清晰度与内存）。
_DEFAULT_SCALE = 2.0
_MAX_SCALE = 4.0


class CourseMediaService:
    """课程资料媒体（进程级单例）。"""

    _instance: "CourseMediaService | None" = None

    @classmethod
    def get_instance(cls) -> "CourseMediaService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 原始文件 ────────────────────────────────────────
    @staticmethod
    def _document_row(document_id: str):
        """读取文档行，不存在抛 1001。"""
        row = get_db().query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
        if row is None:
            raise AppError(1001, "文档不存在")
        return row

    @staticmethod
    def _safe_path(row) -> Path:
        """把 ``original_path`` 解析为 ``files_dir`` 下的绝对路径（防目录穿越）。"""
        rel = row["original_path"]
        if not rel:
            raise AppError(1002, "该材料没有原始文件", "在线抓取或纯文本材料无法渲染原页")
        base = get_config().files_dir.resolve()
        path = (base / rel).resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise AppError(1000, "文件路径不合法") from exc
        if not path.exists():
            raise AppError(1001, "原始文件不存在", "可能已被清理或移动到别处")
        return path

    def raw_file(self, document_id: str) -> tuple[Path, str]:
        """返回 ``(文件路径, 格式)``；仅 PDF 支持原页渲染。"""
        row = self._document_row(document_id)
        if row["fmt"] != "pdf":
            raise AppError(3000, "只有 PDF 支持原页渲染", f"当前格式：{row['fmt']}")
        return self._safe_path(row), row["fmt"]

    def page_exists(self, document_id: str, page_no: int) -> bool:
        """材料是否存在该页（用于图片题的可用性判断）。"""
        row = self._document_row(document_id)
        return page_no is not None and 1 <= int(page_no) <= int(row["page_count"] or 0)

    # ── 页渲染 ──────────────────────────────────────────
    @staticmethod
    def render_available() -> bool:
        """服务端页渲染是否可用（装了 PyMuPDF 才可用）。"""
        try:
            import fitz  # noqa: F401
            return True
        except Exception:  # noqa: BLE001 - 可选依赖
            return False

    def render_page(self, document_id: str, page_no: int, scale: float = _DEFAULT_SCALE) -> bytes:
        """把 PDF 第 ``page_no`` 页渲染为 PNG 字节（需要 PyMuPDF）。

        Args:
            document_id: 材料 id。
            page_no: 页码（1 起）。
            scale: 缩放倍数（≈ ``scale * 72`` DPI）。

        Returns:
            PNG 字节。

        Raises:
            AppError: 3000 非 PDF；1001 文件缺失；3001 未装 PyMuPDF 或渲染失败。
        """
        row = self._document_row(document_id)
        if row["fmt"] != "pdf":
            raise AppError(3000, "只有 PDF 支持原页渲染", f"当前格式：{row['fmt']}")
        path = self._safe_path(row)
        if not (1 <= int(page_no) <= int(row["page_count"] or 0)):
            raise AppError(1000, "页码超出范围", f"该材料共 {row['page_count'] or 0} 页")
        try:
            import fitz  # PyMuPDF（AGPL，默认不装）
        except Exception as exc:  # noqa: BLE001 - 可选依赖
            raise AppError(
                3001,
                "服务端页渲染组件未安装",
                "前端会用内置 pdf.js 渲染材料页，功能不受影响；"
                "如需服务端出图请自行安装 PyMuPDF（注意其 AGPL 许可）",
            ) from exc
        try:
            zoom = max(1.0, min(float(scale or _DEFAULT_SCALE), _MAX_SCALE))
            with fitz.open(str(path)) as doc:
                page = doc.load_page(int(page_no) - 1)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                return pix.tobytes("png")
        except Exception as exc:  # noqa: BLE001 - 渲染失败给出可读错误
            logger.warning("PDF 页渲染失败：%s", type(exc).__name__)
            raise AppError(3001, "页面渲染失败", type(exc).__name__) from exc

    @staticmethod
    def conversation_markdown(conversation_id: str) -> str:
        """把课堂对话导出为 Markdown（P1：导出对话记录）。"""
        db = get_db()
        row = db.query_one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if row is None:
            raise AppError(1001, "会话不存在")
        lines = [f"# {row['title'] or '课堂对话'}", "",
                 f"> 导出时间：{row['updated_at']}", ""]
        for m in db.query_all(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid",
            (conversation_id,),
        ):
            who = "我" if m["role"] == "user" else "啃书先生"
            lines.append(f"**{who}**：{m['content']}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def lesson_markdown(lesson: dict[str, Any]) -> str:
        """把讲次（讲义 + 练习作答）导出为 Markdown。"""
        lines = [f"# {lesson.get('title') or '讲次'}", ""]
        if lesson.get("objective"):
            lines += [f"> 目标：{lesson['objective']}", ""]
        if lesson.get("board_md"):
            lines += [lesson["board_md"], ""]
        db = get_db()
        questions = db.query_all(
            "SELECT * FROM practice_questions WHERE lesson_id = ? ORDER BY ordinal",
            (lesson["id"],),
        )
        if questions:
            lines += ["## 随堂练习", ""]
            latest: dict[str, Any] = {}
            for a in db.query_all(
                "SELECT * FROM practice_attempts WHERE lesson_id = ? ORDER BY created_at",
                (lesson["id"],),
            ):
                latest[a["question_id"]] = a
            for q in questions:
                att = latest.get(q["id"])
                lines.append(f"**{q['ordinal']}. {q['stem']}**")
                lines.append("")
                if att is not None:
                    lines.append(f"- 我的作答：{att['answer']}")
                    lines.append(f"- 结果：{'正确' if att['correct'] else '不正确'}（{att['score']}）")
                if q["explanation"]:
                    lines.append(f"- 解析：{q['explanation']}")
                lines.append("")
        return "\n".join(lines)


def get_course_media_service() -> CourseMediaService:
    """返回课程媒体服务单例。"""
    return CourseMediaService.get_instance()
