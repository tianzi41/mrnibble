"""代码审查（2026-09-21）修复项的回归护栏。

覆盖本轮从审查报告里**采纳并修改**的四类问题（其余项在报告里被判定为不成立或暂不做）：

[U] P1-2 上传大小预检：超限文件必须在 `await upload.read()` **之前**被跳过
    （原实现会把整个文件读进内存，再由服务层拒绝 —— 误拖 1GB 文件即顶内存）。
[F] P1-3 网页抓取限大小：超过 20MB 的页面要中止并给出可读原因（原来 `resp.text` 全量入内存）。
[H] P2-3（低风险子集）安全响应头：`X-Content-Type-Options` / `Referrer-Policy`。
[C] P2-2 监听地址只允许回环（把「绝不绑 0.0.0.0」的红线做成可执行约束）。
[T] 另把审查报告 **P2-7（FTS 特殊字符）的证伪**固化成断言 —— 它其实**不成立**
    （`_escape_phrase` 已把 `"` 转义并用引号包裹整段），但值得长期盯着。

进程内跑：ASGI 直连（不开端口）+ 一个本地小 HTTP 服务，独立临时 MRNIBBLE_DATA_DIR。

用法::

    .venv/Scripts/python.exe dev/hardening_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from fastapi import FastAPI, File, UploadFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TMP = ROOT / ".tmp" / ("test-data-hardening-%d" % int(time.time()))
PORT = 8796

# 极小的探针应用：只用来证明「Starlette 解析 multipart 时会填 upload.size」。
# 必须定义在**模块级**：`from __future__ import annotations` 让注解变成字符串，
# 定义在函数里的话 Pydantic 解析不到局部别名（实测报 class-not-fully-defined）。
_probe_app = FastAPI()


@_probe_app.post("/p")
async def _probe_sizes(files: list[UploadFile] = File(...)) -> dict:
    return {"sizes": [f.size for f in files]}

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"    {detail}" if detail and not cond else ""))


def section(title: str) -> None:
    print(f"\n{title}")


BIG_PAGE = 25 * 1024 * 1024      # 超过抓取上限（20MB）
Q = chr(34)


class _Page(BaseHTTPRequestHandler):
    """/big 吐 25MB，其它路径吐一个小页面。"""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/big"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            chunk = b"<p>" + b"x" * (64 * 1024) + b"</p>"
            sent = 0
            while sent < BIG_PAGE:
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                sent += len(chunk)
            return
        body = ("<html><head><title>小页面</title></head><body>"
                f"<p>你好 {self.path}</p></body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        return


class _StubFile:
    """给 UploadFile 当底层的假文件对象：能证明「有没有被读」。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.reads = 0
        self.payload = (b"a" * 16) + str(time.time()).encode()   # 内容带时间戳，避免重复文件判定

    def read(self, n: int = -1) -> bytes:
        self.reads += 1
        if self.mode == "boom":
            raise AssertionError("预检失效：超限文件被读进内存了")
        return self.payload if n in (-1, None) else self.payload[:n]


async def _run() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    (TMP / "logs").mkdir(parents=True, exist_ok=True)
    os.environ["MRNIBBLE_DATA_DIR"] = str(TMP)
    for junk in TMP.glob("fts_probe.db*"):
        junk.unlink(missing_ok=True)

    import httpx  # noqa: F401 - 模块级已导入，此处保留兼容
    from fastapi import BackgroundTasks
    from starlette.datastructures import UploadFile as SU

    from backend.config import get_config, reload_config
    from backend.db.connection import get_db
    from backend.errors import AppError
    from backend.main import create_app
    from backend.utils.textutil import cjk_query

    get_db().migrate()   # 临时库先建表

    # ══════════ [U] 上传大小预检 ══════════
    section("[U] P1-2 上传大小预检（超限文件不得被读进内存）")
    from backend.routers.documents import upload_documents

    cfg = get_config()
    over = (cfg.max_upload_mb + 1) * 1024 * 1024
    f_big = _StubFile("boom")
    u_big = SU(file=f_big, size=over, filename="huge.txt")
    try:
        res = await upload_documents(BackgroundTasks(), files=[u_big], collection=None)
        data = res["data"]
    except Exception as e:  # noqa: BLE001 - 预检失效时桩件会抛，这里记成失败而不是崩掉脚本
        data = {"documents": [], "skipped": []}
        check("U1 超限文件在读之前就被跳过（read 从未被调用）", False,
              f"读取被触发：{type(e).__name__}: {e}")
    else:
        check("U1 超限文件在读之前就被跳过（read 从未被调用）",
              f_big.reads == 0 and not data["documents"] and bool(data["skipped"]),
              f"reads={f_big.reads} data={data}")
    check("U1b 跳过原因写明「文件过大」",
          "过大" in json.dumps(data, ensure_ascii=False), str(data))

    f_ok = _StubFile("ok")
    u_ok = SU(file=f_ok, size=len(f_ok.payload), filename="ok.txt")
    res2 = await upload_documents(BackgroundTasks(), files=[u_ok], collection=None)
    check("U2 未超限的文件仍走正常入库路径（read 被调用且有文档）",
          f_ok.reads >= 1 and len(res2["data"]["documents"]) == 1,
          str(res2["data"])[:200])

    # 预检的前提：multipart 解析会填 upload.size（不能靠猜）
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_probe_app),
                                 base_url="http://t") as c:
        r = await c.post("/p", files=[("files", ("x.bin", b"b" * (3 * 1024 * 1024),
                                                 "application/octet-stream"))])
    check("U3 Starlette 确实会填 upload.size（预检的前提，实测而非假设）",
          r.json()["sizes"] == [3 * 1024 * 1024], str(r.json()))

    # 端到端：把上限压到 1MB，真传一个 3MB 文件
    os.environ["MRNIBBLE_MAX_UPLOAD_MB"] = "1"
    reload_config()
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/api/documents/upload",
                         files=[("files", ("big.txt", b"z" * (3 * 1024 * 1024), "text/plain"))])
        h = await c.get("/api/health")
    d = r.json()["data"]
    check("U4 端到端（上限 1MB + 3MB 文件）→ 跳过且不入库",
          not d["documents"] and "过大" in d["skipped"][0]["reason"], str(d)[:200])
    leftovers = [p.name for p in get_config().files_dir.glob("*.txt")
                 if p.stat().st_size > 1024 * 1024]
    check("U4b 磁盘没留下超限文件的残留", not leftovers, str(leftovers))
    os.environ.pop("MRNIBBLE_MAX_UPLOAD_MB")
    reload_config()

    # ══════════ [H] 安全响应头 ══════════
    section("[H] P2-3 安全响应头（低风险子集）")
    check("H1 /api/health 带 X-Content-Type-Options: nosniff",
          h.headers.get("x-content-type-options") == "nosniff",
          str(dict(h.headers)))
    check("H2 /api/health 带 Referrer-Policy: no-referrer",
          h.headers.get("referrer-policy") == "no-referrer")

    # ══════════ [C] 监听地址回环校验 ══════════
    section("[C] P2-2 只允许绑回环地址")
    for bad in ("0.0.0.0", "192.168.1.5", "::"):
        os.environ["MRNIBBLE_HOST"] = bad
        try:
            reload_config()
            check(f"C {bad} 被拒绝", False, "居然通过了")
        except ValueError as e:
            check(f"C {bad} 被拒绝并说明原因", "回环" in str(e), str(e)[:80])
    os.environ.pop("MRNIBBLE_HOST")
    check("C 回环地址仍可用", reload_config().host == "127.0.0.1")

    # ══════════ [F] 网页抓取限大小 ══════════
    section("[F] P1-3 网页抓取限大小（20MB）")
    srv = HTTPServer(("127.0.0.1", PORT), _Page)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from backend.services.ingest import get_ingest_service

    svc = get_ingest_service()
    # 每次跑用不同的 query（页面正文里带 path）→ 内容不同、hash 不同，重复跑不会撞去重
    url_small = f"http://127.0.0.1:{PORT}/small?ts={int(time.time())}"
    try:
        try:
            svc.create_from_url(f"http://127.0.0.1:{PORT}/big")
            check("F1 超大页面被中止", False, "居然成功了")
        except AppError as e:
            txt = f"{e.message or ''} {e.detail or ''}"
            check("F1 超大页面（25MB）→ 3003 且说清「过大」",
                  e.code == 3003 and "过大" in txt, f"{e.code}/{txt.strip()}")
        try:
            url_doc = svc.create_from_url(url_small)
            check("F2 正常小页面仍能抓取入库（无回归）", bool(url_doc), str(url_doc))
        except AppError as e:
            check("F2 正常小页面仍能抓取入库（无回归）", False, f"{e.code}/{e.message}")

        # F3/F4：重复抓同一页面 —— 旧实现是 UNIQUE(file_hash) 裸抛 IntegrityError（500）
        #       且因为「先写文件再 INSERT」，还会在 files/ 留下孤儿文件
        files_before = sorted(p.name for p in get_config().files_dir.glob("*.html"))
        try:
            svc.create_from_url(url_small)
            check("F3 重复抓同一页面给出可读提示（3004 已存在）", False, "居然又入了一次")
        except AppError as e:
            check("F3 重复抓同一页面给出可读提示（3004 已存在）",
                  e.code == 3004 and "已在" in (e.message or ""), f"{e.code}/{e.message}")
        except Exception as e:  # noqa: BLE001 - 旧实现会裸抛 IntegrityError，记成失败
            check("F3 重复抓同一页面给出可读提示（3004 已存在）", False,
                  f"{type(e).__name__}: {e}")
        files_after = sorted(p.name for p in get_config().files_dir.glob("*.html"))
        check("F4 重复抓取没有留下孤儿文件", files_before == files_after,
              f"{len(files_before)} → {len(files_after)}")
    finally:
        srv.shutdown()

    # ══════════ [T] FTS 特殊字符（P2-7 的证伪固化为断言）══════════
    section("[T] P2-7 FTS5 特殊字符（审查报告怀疑未转义 —— 实测不成立）")
    nasty = ["洛必达法则", "极限 delta", Q, "*", Q * 2, "a* b(", "NEAR OR AND NOT",
             "lim" + Q + "it", "x OR y", "())", Q + "洛" + Q, chr(92), "%", "_",
             chr(0x3000), "a AND b", "极限" + Q]
    bad_q = [q for q in nasty if cjk_query(q).count(Q) % 2]
    check("T1 畸形查询词的 FTS 表达式引号始终成对", not bad_q, str(bad_q))

    con = sqlite3.connect(str(TMP / "fts_probe.db"))
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS t USING fts5(body)")
    con.execute("INSERT INTO t(body) VALUES (?)", ("洛 必 达 法 则 极 限 delta",))
    errs: list[str] = []
    for q in nasty:
        try:
            con.execute("SELECT rowid FROM t WHERE t MATCH ?", (cjk_query(q),)).fetchall()
        except Exception as e:  # noqa: BLE001
            errs.append(f"{q!r}: {type(e).__name__}: {e}")
    con.close()
    check("T2 真 FTS5 上跑全部畸形查询 0 异常", not errs, str(errs[:3]))

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  失败：" + f)
    sys.stdout.flush()
    os._exit(0 if not FAIL else 1)   # 硬退出：见 launcher_liveness_check 的同类坑


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
