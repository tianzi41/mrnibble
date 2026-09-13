"""T01/T02 自检脚本：路径定位 + 建库幂等 + 中文 FTS 命中。

运行：
    cd <项目根>
    PYTHONPATH=src .venv/Scripts/python.exe dev/t01_t02_check.py

断言全部通过时退出码为 0。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 允许直接 `python dev/t01_t02_check.py`（把 src 加入 sys.path）。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.paths import (  # noqa: E402
    data_path,
    ensure_data_dirs,
    project_root,
    resource_path,
)
from backend.utils.textutil import (  # noqa: E402
    build_index_text,
    cjk_query,
    segment,
)

PASSED: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    """断言并记录结果。"""
    mark = "PASS" if condition else "FAIL"
    line = f"[{mark}] {name}" + (f"  -> {extra}" if extra else "")
    print(line)
    if not condition:
        raise SystemExit(f"断言失败：{name} {extra}")
    PASSED.append(name)


def check_resource_paths() -> None:
    """开发态与模拟冻结态都应能定位 web/。"""
    web_dev = resource_path("web", "index.html")
    check("resource_path 开发态定位 web/index.html", web_dev.exists(), str(web_dev))

    # 用临时目录模拟 PyInstaller 冻结态：_MEIPASS 指向含 web/ 的目录。
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp)
        (fake / "web").mkdir(parents=True)
        (fake / "web" / "index.html").write_text("frozen", encoding="utf-8")
        (fake / "backend" / "db").mkdir(parents=True)
        (fake / "backend" / "db" / "schema.sql").write_text("--", encoding="utf-8")
        old = getattr(sys, "_MEIPASS", None)
        sys._MEIPASS = str(fake)  # type: ignore[attr-defined]
        try:
            frozen_web = resource_path("web", "index.html")
            check(
                "resource_path 冻结态(_MEIPASS)定位 web/index.html",
                frozen_web.exists() and frozen_web.read_text(encoding="utf-8") == "frozen",
                str(frozen_web),
            )
            frozen_schema = resource_path("backend", "db", "schema.sql")
            check("resource_path 冻结态定位 schema.sql", frozen_schema.exists(), str(frozen_schema))
        finally:
            if old is None:
                del sys._MEIPASS  # type: ignore[attr-defined]
            else:
                sys._MEIPASS = old  # type: ignore[attr-defined]

    check("data_path 定位 data/", str(data_path()).endswith("data"), str(data_path()))
    check("project_root 正确", project_root() == ROOT, str(project_root()))


def check_db_idempotent() -> None:
    """重复迁移应幂等，且 FTS/触发器齐备。"""
    from backend.db.connection import get_db

    ensure_data_dirs()
    db = get_db()

    def snapshot() -> set[str]:
        rows = db.query_all(
            "SELECT name FROM sqlite_master WHERE type IN ('table','trigger','index')"
        )
        return {r["name"] for r in rows}

    db.migrate()
    names_1 = snapshot()
    v1 = db.query_one("SELECT value FROM schema_meta WHERE key='schema_version'")["value"]
    db.migrate()
    db.migrate()
    names_2 = snapshot()
    v2 = db.query_one("SELECT value FROM schema_meta WHERE key='schema_version'")["value"]

    check("重复迁移表对象集合不变（幂等）", names_1 == names_2)
    check("schema_version 稳定为 1", v1 == "1" and v2 == "1", f"{v1}/{v2}")
    check(
        "FTS 虚表与触发器齐备",
        {"chunks_fts", "memories_fts", "trg_chunks_ai", "trg_chunks_ad", "trg_chunks_au",
         "trg_mem_ai", "trg_mem_ad", "trg_mem_au"}.issubset(names_2),
    )


def check_chinese_fts() -> None:
    """逐字 CJK 索引 + 短语查询：2 字 / 3 字 / 中英混排命中，负例为 0。"""
    import sqlite3

    doc = "洛必达法则是求未定式极限的重要方法。The epsilon-delta definition of limit is key."
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE idx USING fts5(body, tokenize='unicode61')")
    conn.execute("INSERT INTO idx(body) VALUES (?)", (build_index_text(doc),))

    def hits(query: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM idx WHERE idx MATCH ?", (cjk_query(query),)
        ).fetchone()
        return int(row[0])

    check("中文 2 字查询「极限」命中", hits("极限") >= 1, f"idx={build_index_text('极限')!r}")
    check("中文 3 字查询「洛必达」命中", hits("洛必达") >= 1)
    check("中文 4 字查询「未定式」命中", hits("未定式") >= 1)
    check("英文整词查询「delta」命中", hits("delta") >= 1)
    check("中英混排查询命中", hits("极限 delta") >= 1)
    check("负例「不存在词」命中为 0", hits("量子纠缠退相干") == 0)
    check("jieba segment 可用", len(segment("极限与连续").split()) >= 1, segment("极限与连续"))
    conn.close()


def main() -> int:
    print(f"项目根：{ROOT}")
    print(f"PYTHONPATH: {os.environ.get('PYTHONPATH', '')}")
    print("-" * 60)
    check_resource_paths()
    check_db_idempotent()
    check_chinese_fts()
    print("-" * 60)
    print(f"全部通过：{len(PASSED)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
