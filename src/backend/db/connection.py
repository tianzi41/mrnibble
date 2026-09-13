"""SQLite 连接管理（线程局部连接 + WAL + 外键）。

设计要点：
- 单机单人、单进程；FastAPI 的同步端点在线程池执行，故采用**线程局部连接**
  （每个线程复用一条连接，避免跨线程共享同一 sqlite3 连接）。
- 统一开启 ``journal_mode=WAL``（并发读）、``foreign_keys=ON``（级联删除）、
  ``busy_timeout=5000``（写锁等待）。
- ``row_factory = sqlite3.Row``，读取按列名取值。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import get_config
from .migrations import run_migrations

__all__ = ["Database", "get_db"]


class Database:
    """SQLite 数据库封装（线程安全：每线程一条连接）。

    Attributes:
        db_path: 主库文件路径。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path: Path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()

    # ── 连接 ────────────────────────────────────────────
    def _new_conn(self) -> sqlite3.Connection:
        """创建并配置一条新连接。"""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            isolation_level=None,  # 自动提交；写操作显式管理
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def get_conn(self) -> sqlite3.Connection:
        """获取当前线程的连接（不存在则创建）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        """上下文管理器：产出游标，异常时回滚。"""
        conn = self.get_conn()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务块：正常提交，异常回滚。"""
        conn = self.get_conn()
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def executescript(self, sql: str) -> None:
        """执行多语句脚本（用于建表 DDL）。"""
        conn = self.get_conn()
        conn.executescript(sql)

    def query_all(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        """执行查询并返回全部行。"""
        cur = self.get_conn().execute(sql, params)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        """执行查询并返回首行（无结果返回 ``None``）。"""
        cur = self.get_conn().execute(sql, params)
        try:
            return cur.fetchone()
        finally:
            cur.close()

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """执行写语句，返回游标（调用方可读取 ``lastrowid``/``rowcount``）。"""
        return self.get_conn().execute(sql, params)

    # ── 生命周期 ────────────────────────────────────────
    def init_schema(self) -> None:
        """执行 ``schema.sql``（幂等建表/建触发器）。"""
        from ..paths import resource_path

        schema_file = resource_path("backend", "db", "schema.sql")
        sql = schema_file.read_text(encoding="utf-8")
        self.executescript(sql)

    def migrate(self) -> None:
        """建库并执行版本化迁移（幂等）。"""
        run_migrations(self)

    def checkpoint(self) -> None:
        """执行 WAL checkpoint，把 WAL 落盘（停机时调用）。"""
        try:
            self.get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.Error:
            pass

    def close_all(self) -> None:
        """关闭当前线程连接（进程退出前调用）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None


# ── 单例 ────────────────────────────────────────────────
_instance: Database | None = None
_instance_lock = threading.Lock()


def get_db() -> Database:
    """返回进程级 :class:`Database` 单例。"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = Database(get_config().db_path)
    return _instance
