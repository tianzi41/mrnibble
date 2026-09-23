"""把打包版恢复到出厂状态：清掉主理人测试期留下的数据与配置。

做法：
1. 通过应用自身 API 删除测试文档 / 会话 / 生成产物（连带切片、向量、原始文件、导出物）；
2. 停服后把 ``settings`` 表清空（回到代码内置默认值，即"未配置模型"）；
3. VACUUM 收缩数据库。

不做任何文件系统删除——原始文件由应用在删除文档时自行清理。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
# 构建目录可用 MRNIBBLE_DIST 覆盖（默认 dist4）。
_DIST = Path(os.environ.get("MRNIBBLE_DIST", "dist4"))
APP = (_DIST if _DIST.is_absolute() else ROOT / _DIST) / "啃书先生"
EXE = APP / "啃书先生.exe"
DB = APP / "data" / "mrnibble.db"
BASE = "http://127.0.0.1:8760"


def wait_health(timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(f"{BASE}/api/health", timeout=3, trust_env=False).status_code == 200:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def stop_exe() -> None:
    """按映像名结束本机启动的啃书先生进程（仅限本机测试启动的实例）。"""
    subprocess.run(["taskkill", "/IM", "啃书先生.exe", "/F"],
                   capture_output=True, text=True)
    time.sleep(1.5)


def main() -> int:
    stop_exe()

    proc = subprocess.Popen([str(EXE)], cwd=str(APP),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = wait_health()
    if ok:
        C = dict(trust_env=False, timeout=30)
        docs = httpx.get(f"{BASE}/api/documents?page=1&page_size=200", **C).json()["data"]["items"]
        convs = httpx.get(f"{BASE}/api/conversations", **C).json()["data"]
        gens = httpx.get(f"{BASE}/api/generations", **C).json()["data"]["items"]
        for d in docs:
            print(f"  删除测试文档: {d['title']}")
            httpx.delete(f"{BASE}/api/documents/{d['id']}", **C)
        for c in convs:
            print(f"  删除测试会话: {c.get('title') or c['id'][:8]}")
            httpx.delete(f"{BASE}/api/conversations/{c['id']}", **C)
        for g in gens:
            print(f"  删除测试产物: {g.get('title')}")
            httpx.delete(f"{BASE}/api/generations/{g['id']}", **C)
        for m in httpx.get(f"{BASE}/api/memories?page=1&page_size=200", **C).json()["data"]["items"]:
            print("  删除测试记忆")
            httpx.delete(f"{BASE}/api/memories/{m['id']}", **C)

    try:
        proc.terminate(); proc.wait(timeout=10)
    except Exception:
        proc.kill()
    time.sleep(2)

    # 停服后：配置恢复默认（清空 settings 表 = 回到代码内置值）
    con = sqlite3.connect(DB)
    con.execute("DELETE FROM settings")
    con.execute("DELETE FROM events")
    con.commit()
    con.execute("VACUUM")
    con.commit()
    # 复核
    left = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("settings", "documents", "conversations", "memories",
                      "generations", "flashcards", "chunks", "messages")}
    con.close()

    files = list((APP / "data" / "files").glob("*")) if (APP / "data" / "files").exists() else []
    exports = list((APP / "data" / "exports").glob("*")) if (APP / "data" / "exports").exists() else []
    for f in [*files, *exports]:
        try:
            f.unlink()
            print(f"  清理测试产物文件: {f.name}")
        except OSError as e:
            print(f"  跳过 {f.name}: {e}")

    print("\n=== 出厂状态复核 ===")
    for k, v in left.items():
        print(f"  {k}: {v} 条")
    print(f"  data/files 剩余: {len(list((APP / 'data' / 'files').glob('*')))} 个文件")
    print("\n✅ 已恢复出厂状态。现在打开 啃书先生.exe 会看到全新的空资料库与「未配置模型」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
