"""版本化数据库迁移（幂等）。

策略：
- `schema_meta` 保存 ``schema_version`` 与 ``created_at`` 等元信息；
- 首次建库执行 ``schema.sql``；后续按版本号顺序补丁（当前为 v1，无补丁）；
- 每次启动都执行一次 :func:`run_migrations`，保证「重复启动幂等」。

用法::

    from backend.db.connection import Database
    db = Database(path)
    db.migrate()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..utils.timeutil import now_iso

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from .connection import Database

__all__ = ["SCHEMA_VERSION", "run_migrations"]

# 当前目标 schema 版本。新增 DDL 时 +1，并在此文件追加升级逻辑。
SCHEMA_VERSION: int = 5


def _get_version(db: "Database") -> int:
    """读取当前 schema 版本（不存在视为 0）。"""
    row = db.query_one("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    if row is None or row["value"] is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _set_meta(db: "Database", key: str, value: str) -> None:
    """写入/更新 ``schema_meta`` 的一条键值。"""
    db.execute(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _column_exists(db: "Database", table: str, column: str) -> bool:
    """判断表中是否已存在某列（用于幂等地给旧库补列）。"""
    rows = db.query_all(f"PRAGMA table_info({table})")
    return any(r["name"] == column for r in rows)


def _v2_lesson_chat(db: "Database") -> None:
    """v2：课程课堂需要把消息归属到具体讲次（``messages.lesson_id``）。

    旧库没有该列，这里按列存在性判断后补列；已有库重复启动不会报错。
    """
    if not _column_exists(db, "messages", "lesson_id"):
        db.execute("ALTER TABLE messages ADD COLUMN lesson_id TEXT")


def _v3_course_p1(db: "Database") -> None:
    """v3：P1 需要的课程域扩展。

    - ``course_lessons.board_marks``：材料标注（高亮/圈注/连线旁注）；
    - ``practice_questions.image``：图片题（渲染材料页）；
    - ``practice_attempts.attempt_no``：第几次作答（单元总结与重做需要）；
    - ``course_units.summary_*``：单元完成后的总结。
    """
    for table, column, ddl in (
        ("course_lessons", "board_marks", "TEXT"),
        ("practice_questions", "image", "TEXT"),
        ("practice_attempts", "attempt_no", "INTEGER NOT NULL DEFAULT 1"),
        ("course_units", "summary_json", "TEXT"),
        ("course_units", "summary_md", "TEXT"),
        ("course_units", "summary_status", "TEXT DEFAULT 'pending'"),
        ("course_units", "summary_error", "TEXT"),
    ):
        if not _column_exists(db, table, column):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _v4_courseware_split(db: "Database") -> None:
    """v4：课件与讲师讲稿拆分存储。

    - ``course_lessons.slides_json``：学生看的逐页课件；
    - ``course_lessons.script_json``：讲师朗读讲稿，按 ``slide_id`` 关联课件。
    """
    for column in ("slides_json", "script_json"):
        if not _column_exists(db, "course_lessons", column):
            db.execute(f"ALTER TABLE course_lessons ADD COLUMN {column} TEXT")


def _v5_course_hands_on(db: "Database") -> None:
    """v5：课程级「实践环节」开关（``courses.hands_on``）。

    并非每门课都需要实操（文言文、理论类课程关掉即可）。默认 1 →
    存量课程行为不变（可出 hands_on 题、讲稿可布置真实操作任务）。
    """
    if not _column_exists(db, "courses", "hands_on"):
        db.execute("ALTER TABLE courses ADD COLUMN hands_on INTEGER NOT NULL DEFAULT 1")


def run_migrations(db: "Database") -> int:
    """执行建库与迁移，返回迁移后的 schema 版本。

    Args:
        db: 数据库封装。

    Returns:
        迁移完成后的 ``SCHEMA_VERSION``。
    """
    # 1) 建基础表（幂等）。
    db.init_schema()
    current = _get_version(db)

    # 2) 版本补丁（v1 为基线，无补丁）。未来在此按 current 顺序追加。
    if current == 0:
        _set_meta(db, "created_at", now_iso())
    if current < 2:
        _v2_lesson_chat(db)
    if current < 3:
        _v3_course_p1(db)
    if current < 4:
        _v4_courseware_split(db)
    if current < 5:
        _v5_course_hands_on(db)

    # 3) 落版本与更新时间。
    if current != SCHEMA_VERSION:
        _set_meta(db, "schema_version", str(SCHEMA_VERSION))
    _set_meta(db, "updated_at", now_iso())
    return SCHEMA_VERSION
