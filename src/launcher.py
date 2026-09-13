"""知伴桌面启动器（架构文档 §1.2 / T14）。

职责：
1. 选一个可用端口并启动本机 HTTP 服务（uvicorn，仅回环地址）；
2. 用系统自带 Edge 的 ``--app`` 模式打开无边框应用窗口（零额外依赖；
   Edge 不存在时退化到默认浏览器）；
3. 等待用户关闭窗口，然后优雅停机（WAL checkpoint + 释放端口）。

**不依赖任何开发者环境**：PyInstaller 冻结后本文件与后端一同打进
``知伴.exe``，目标机器只需解压双击。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener, urlopen

APP_TITLE = "知伴 ZhiBan"
START_TIMEOUT = 30.0
POLL_INTERVAL = 0.3

# ── 服务存活策略（2026-09-12 修复「首开必显示拒绝连接」）─────────
# 曾以「浏览器子进程退出」=「用户关窗」，但 Edge 首开可能把 URL 转交给
# 已有实例后**立即退出**，导致服务启动 1 秒就被关掉。现改为以**页面心跳**
# 为准（前端每 5 秒 POST /api/heartbeat）：
HEARTBEAT_GRACE = 15.0     # 收到过心跳后：静默超过此值 → 页面已关，退出
STARTUP_PATIENCE = 45.0    # 从未收到心跳：等页面上线的耐心（冷启动较慢）
REOPEN_AFTER = 8.0         # 窗口进程「秒退」且无心跳：等这么久就补开一次
MAX_REOPEN = 2             # 最多补开次数


def _find_free_port(preferred: int = 8760, max_tries: int = 10) -> int:
    """从 ``preferred`` 起顺延寻找可用端口（与 backend.config.select_port 口径一致）。"""
    for port in range(preferred, preferred + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return 0  # 0 = 让系统随机分配


def _wait_ready(port: int, timeout: float) -> bool:
    """轮询健康检查直到服务就绪。

    显式走**直连**（空代理表）：本机若开着系统代理（Clash/v2rayN 等），
    ``urlopen`` 会信任 ``HTTP_PROXY`` 把 127.0.0.1 的请求也丢给代理，
    健康检查结果就不可信了。
    """
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    opener = build_opener(ProxyHandler({}))
    while time.time() < deadline:
        try:
            with opener.open(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(POLL_INTERVAL)
    return False


def _open_window(port: int) -> subprocess.Popen | None:
    """优先用 Edge ``--app`` 打开应用窗口；失败则退回默认浏览器。

    ``--no-proxy-server``：全部内容都在 127.0.0.1，直连即可；带系统代理
    反而可能把 localhost 请求丢给代理（装了 Clash 的机器上首开就会
    「拒绝连接」）。
    """
    url = f"http://127.0.0.1:{port}"
    for name in ("msedge.exe", "chrome.exe"):
        exe = shutil.which(name)
        if not exe:
            for candidate in (
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
                / "Microsoft/Edge/Application/msedge.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "Microsoft/Edge/Application/msedge.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "Google/Chrome/Application/chrome.exe",
            ):
                if candidate.exists():
                    exe = str(candidate)
                    break
        if exe:
            try:
                return subprocess.Popen(
                    [exe, f"--app={url}", f"--user-data-dir={_profile_dir()}",
                     "--no-first-run", "--no-default-browser-check", "--no-proxy-server"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                continue
    # 兜底：默认浏览器
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass
    return None


def _profile_dir() -> Path:
    """浏览器窗口的独立用户数据目录（避免污染用户默认配置）。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent / "data"
    else:
        base = Path(__file__).resolve().parents[1] / "data"
    d = base / "browser-profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _serve_until_close(port: int, window: subprocess.Popen | None) -> None:
    """保持服务运行，直到页面心跳停止（= 应用窗口已关闭）。

    判定规则（详见模块顶部注释）：

    - 窗口进程还活着 → 无条件继续服务（与旧行为一致，页面可能只是慢）；
    - 窗口进程退出 + 心跳仍持续 → 是「转交给已有实例」，继续服务；
    - 收到过心跳 + 心跳静默超过 ``HEARTBEAT_GRACE`` → 用户已关窗，退出；
    - 窗口进程**秒退**（存活 < 5s，首开竞争/崩溃）且从未有心跳 →
      补开窗口（最多 ``MAX_REOPEN`` 次）；仍无心跳则放弃退出。
    """
    from backend import heartbeat

    opened_at = time.time()
    exited_at: float | None = None
    lived: float | None = None
    retries = 0

    while True:
        if window is not None:
            try:
                window.wait(timeout=1.0)
                exited_at = time.time()
                lived = exited_at - opened_at
                window = None
                if lived < 5.0:
                    print("[ZhiBan] 浏览器进程很快退出（可能转交给了已有实例），以页面心跳为准")
            except subprocess.TimeoutExpired:
                time.sleep(0.5)
                continue

        idle = heartbeat.idle_seconds()
        if heartbeat.ever_seen():
            # 页面上过线：心跳静默 = 页面已关
            if idle > HEARTBEAT_GRACE:
                print("[ZhiBan] 页面心跳已停止，准备退出")
                return
        else:
            # 页面从未上线
            if lived is not None and lived < 5.0 and retries < MAX_REOPEN:
                # 秒退且无心跳：稍等页面（转交场景目标实例可能加载慢），仍无则补开
                if idle > REOPEN_AFTER:
                    retries += 1
                    print(f"[ZhiBan] 未检测到页面，尝试重新打开窗口（{retries}/{MAX_REOPEN}）")
                    window = _open_window(port)
                    if window is not None:
                        opened_at = time.time()
                        lived = None
                        continue
            if idle > STARTUP_PATIENCE:
                print("[ZhiBan] 页面迟迟未上线，退出（可重新打开知伴）")
                return
        time.sleep(0.5)


def main() -> int:
    port = _find_free_port()
    if not port:
        print("[ZhiBan] 未找到可用端口", file=sys.stderr)
        return 1
    os.environ["ZHIBAN_PORT"] = str(port)

    from backend.config import get_config, write_runtime_json
    from backend.main import create_app

    cfg = get_config()
    write_runtime_json(port, cfg)

    import uvicorn

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    if not _wait_ready(port, START_TIMEOUT):
        print("[ZhiBan] 服务启动失败，请查看 data/logs/zhiban.log", file=sys.stderr)
        return 1

    print(f"[ZhiBan] 已启动：http://127.0.0.1:{port}  （关闭窗口即退出）")
    window = _open_window(port)

    try:
        if window is not None:
            _serve_until_close(port, window)
        else:
            # 找不到可用的 Edge/Chrome：保持服务，靠心跳/超时退出
            _serve_until_close(port, None)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.should_exit = True
            t.join(timeout=8)
        except Exception:
            pass
        try:
            if window is not None and window.poll() is None:
                window.terminate()
        except Exception:
            pass
    print("[ZhiBan] 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
