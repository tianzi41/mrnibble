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

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener, urlopen

APP_TITLE = "知伴 ZhiBan"
START_TIMEOUT = 30.0
POLL_INTERVAL = 0.3

# ── 服务存活策略（2026-09-12 修复「首开必显示拒绝连接」）─────────
# 曾以「浏览器子进程退出」=「用户关窗」，但 Edge 首开可能把 URL 转交给
# 已有实例后**立即退出**，导致服务启动 1 秒就被关掉。改为以**页面心跳**
# 为准（前端每 5 秒 POST /api/heartbeat）。
#
# ⚠️ 2026-09-20 二次修复（用户报「挂后台一会儿后台窗口自己没了」）：
# 只靠心跳仍然太薄——实测日志（dist43/data/logs/zhiban.log）显示服务在
# 用户最小化窗口后被**本启动器自己**优雅停掉，而同刻浏览器窗口还在（其
# profile 在 27 秒后才落盘）。原因是：浏览器最小化/被遮挡时 Chromium 会
# 节流甚至冻结隐藏页的 JS 定时器，5 秒心跳被拉长到 >15 秒，于是被判「已关窗」。
# 另外 Popen 拿到的 msedge 进程常因「URL 转交已有实例」而秒退，那个句柄
# 本来就已经失效——此时**只剩心跳这一道防线**。
# 现在补一道**不依赖页面 JS 的硬信号**：枚举顶层窗口，只要能找到标题为
# 「知伴 · 本地 AI 学习伴侣」的窗口，就无条件继续服务（最小化、被遮挡、
# 渲染进程挂起、系统唤醒都不影响）。心跳降级为兜底。
HEARTBEAT_GRACE = 15.0     # 窗口已消失时：心跳静默超过此值 → 退出
HEARTBEAT_GRACE_UNKNOWN = 30.0  # 窗口探测不可用时的宽容值（探测异常才走这里）
STARTUP_PATIENCE = 45.0    # 从未收到心跳：等页面上线的耐心（冷启动较慢）
REOPEN_AFTER = 8.0         # 窗口进程「秒退」且无心跳：等这么久就补开一次
MAX_REOPEN = 2             # 最多补开次数
APP_TITLE_MARK = "知伴 · 本地 AI 学习伴侣"   # index.html 的 <title>，= --app 窗口标题
PROBE_INTERVAL = 1.0       # 窗口探测间隔（秒）
BROWSER_PROCS = ("msedge.exe", "chrome.exe")


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


def _browser_pref() -> str:
    """浏览器偏好：``auto``（默认，Edge 优先）｜``edge``｜``chrome``。

    环境变量 ``ZHIBAN_BROWSER`` 可强制指定，两个用途：
    ① 给偏爱 Chrome（或想验证 Chrome）的用户一个 explicit 出口 ——
       装着 Edge 的机器上自动选择永远走 Edge；
    ② **验证回退路径**：2026-09-22 用户提出"没测过 Chrome 能否正常
       运行"，在装着 Edge 的机器上 `ZHIBAN_BROWSER=chrome` 即可强制
       走 Chrome 跑一遍。
    """
    v = (os.environ.get("ZHIBAN_BROWSER") or "").strip().lower()
    return v if v in ("edge", "chrome") else "auto"


def _browser_candidates() -> list[tuple[str, str, list[Path]]]:
    """按偏好排出候选浏览器：``[(tag, 进程名, [候选 exe...])]``。

    ``tag`` 决定 profile 目录名（见 :func:`_profile_dir`）。``auto`` 时
    Edge 优先（Windows 出厂自带、覆盖最广）；显式偏好排最前，找不到
    再回退另一个 —— 实际用了谁会在 launcher.log 里记（``open_window``）。
    """
    edge_paths = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
    ]
    chrome_paths = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
    ]
    table = {
        "edge": ("edge", "msedge.exe", edge_paths),
        "chrome": ("chrome", "chrome.exe", chrome_paths),
    }
    pref = _browser_pref()
    order = (["edge", "chrome"] if pref == "auto"
             else [pref, "chrome" if pref == "edge" else "edge"])
    return [table[k] for k in order]


def _open_window(port: int) -> subprocess.Popen | None:
    """用 Edge / Chrome 的 ``--app`` 模式打开应用窗口（顺序见 _browser_candidates）。

    ``--no-proxy-server``：全部内容都在 127.0.0.1，直连即可；带系统代理
    反而可能把 localhost 请求丢给代理（装了 Clash 的机器上首开就会
    「拒绝连接」）。

    ``--disable-http-cache``：**必须加**。应用窗口用的是独立 profile
    （``--user-data-dir``），而静态资源没有 ``Cache-Control`` 头 ——
    浏览器于是走「启发式缓存」，把 JS/CSS 当新鲜文件，**改了前端、重启应用看到的还是旧页面**
    （2026-09-18 用户报「新功能看不到」就是它）。而 ``--app`` 模式没有地址栏、
    用户也没有「刷新」按钮可按。本地应用不存在网络开销，直接禁掉缓存最省事。

    ``--autoplay-policy=no-user-gesture-required``：**必须加**。
    新手引导第 1 步的介绍解说要求「打开就自动播」，而 Chromium 系浏览器
    默认禁止无用户手势的媒体自动播放（``audio.play()`` 直接 reject）——
    用户实测「必须手点播放按钮才响」（2026-09-22）。本地软件不存在
    「打扰用户」的顾虑，直接放行；前端仍保留手势兜底与手动按钮。
    以上参数对 Edge / Chrome 同源（都是 Chromium 系），换浏览器不需改。
    """
    url = f"http://127.0.0.1:{port}"
    for tag, proc, candidates in _browser_candidates():
        exe = shutil.which(proc)
        if not exe:
            for cand in candidates:
                if cand.exists():
                    exe = str(cand)
                    break
        if not exe:
            _log_launcher("browser_not_found", browser=tag, proc=proc)
            continue
        try:
            win = subprocess.Popen(
                [exe, f"--app={url}", f"--user-data-dir={_profile_dir(tag)}",
                 "--no-first-run", "--no-default-browser-check", "--no-proxy-server",
                 "--disable-http-cache",
                 "--autoplay-policy=no-user-gesture-required"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _log_launcher("open_window", browser=tag, exe=Path(exe).name, pid=win.pid)
            return win
        except OSError:
            continue
    # 兜底：默认浏览器
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass
    return None


def _data_dir() -> Path:
    """数据目录（口径与 ``backend.paths`` 一致）。

    优先级：``ZHIBAN_DATA_DIR`` 环境变量 > 冻结态 exe 同级的 ``data/`` >
    开发态仓库的 ``data/``。
    """
    env = os.environ.get("ZHIBAN_DATA_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "data"
    return Path(__file__).resolve().parents[1] / "data"


def _profile_dir(tag: str = "edge") -> Path:
    """浏览器窗口的独立用户数据目录（避免污染用户默认配置）。

    **按浏览器分目录**：Edge 与 Chrome 是不同 Chromium 分支，官方不支持
    共用同一个 ``user-data-dir``——用户机器上 Edge 建的 profile 若被
    Chrome 打开，会出不可预期的问题（2026-09-22 修）。
    Edge 沿用历史目录名 ``browser-profile``（已装用户零迁移、无感知），
    Chrome 用 ``browser-profile-chrome``。
    """
    name = "browser-profile" if tag == "edge" else f"browser-profile-{tag}"
    d = _data_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_launcher(msg: str, **fields: Any) -> None:
    """把启动器的生命周期写进 ``data/logs/launcher.log``。

    为什么必须有：启动器原先只 ``print`` 到控制台，而「服务被判定已关窗」
    时用户看到的现象正是**控制台窗口一起消失**——事后完全无迹可查
    （2026-09-20 那次只能靠浏览器 profile 的落盘时间反推窗口先死还是后死）。
    这里落一份 JSON Lines 日志，不打断任何流程（失败静默）。
    """
    try:
        d = _data_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "msg": msg}
        rec.update(fields)
        with (d / "launcher.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _window_titles() -> list[tuple[int, str]]:
    """枚举所有顶层窗口，返回 ``[(pid, 标题), ...]``。

    用 ``ctypes`` 直调 user32，不引入新依赖；**最小化/被遮挡的窗口同样能被枚举到**
    （这正是它比页面心跳可靠的原因）。任何异常都返回空列表，交给调用方兜底。
    """
    out: list[tuple[int, str]] = []
    if sys.platform != "win32":
        return out
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd: int, _lparam: int) -> bool:
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                out.append((int(pid.value), buf.value))
            return True

        user32.EnumWindows(cb_type(_cb), 0)
    except Exception:
        return []
    return out


def _app_window_alive(mark: str = APP_TITLE_MARK,
                      titles: list[tuple[int, str]] | None = None) -> bool | None:
    """应用窗口是否还在（存在标题含 ``mark`` 的浏览器顶层窗口）。

    返回 ``True`` 存活 / ``False`` 已消失 / ``None`` 探测不可用（非 Windows 或
    ctypes 调用失败）——``None`` 时上层退回「只看心跳」的旧策略。
    """
    if titles is None:
        if sys.platform != "win32":
            return None
        titles = _window_titles()
    if not titles:
        # 「真·一个窗口都没有」与「枚举失败」无法区分 —— 保守返回 None
        # （上层会用更长的宽容值，宁可多等一会儿，也不误杀服务）。
        return None
    return any(mark in t for _pid, t in titles)


def _lifetime_action(*, window_proc_alive: bool, app_window_alive: bool | None,
                     idle: float, ever_seen: bool, lived: float | None,
                     retries: int, bye: bool = False) -> str:
    """存活判定（纯函数，便于单测）。

    返回 ``"wait" | "reopen" | "exit-heartbeat" | "exit-startup"``。

    优先级（重要）：
    1. 浏览器进程句柄还活着 → 继续服务（旧行为）；
    2. **应用窗口还在 → 继续服务**（新增的硬信号：最小化/遮挡/渲染挂起都不怕）；
    3. 两者都没了 → 才看心跳：页面主动告别（``bye``）或静默超过宽限 → 退出；
       从未收到过心跳则走「秒退补开 / ``STARTUP_PATIENCE``」。
    """
    if window_proc_alive or app_window_alive:
        return "wait"
    grace = HEARTBEAT_GRACE if app_window_alive is False else HEARTBEAT_GRACE_UNKNOWN
    if ever_seen:
        # 「告别」只在**确认窗口已消失**时才作数：页面刷新（F5 / 重新加载）也会
        # 触发 pagehide，那时窗口还在、或探测不可用，不能因此把服务关掉。
        if (bye and app_window_alive is False) or idle > grace:
            return "exit-heartbeat"
        return "wait"
    if (lived is not None and lived < 5.0 and retries < MAX_REOPEN
            and idle > REOPEN_AFTER):
        return "reopen"
    if idle > STARTUP_PATIENCE:
        return "exit-startup"
    return "wait"


def _serve_until_close(port: int, window: subprocess.Popen | None) -> None:
    """保持服务运行，直到**应用窗口真的不在了**（而不是「心跳听不到」）。

    判定规则（详见模块顶部注释）：

    - 浏览器进程句柄还活着 → 无条件继续服务；
    - **应用窗口还在（窗口标题探测）→ 继续服务** ← 2026-09-20 新增的硬信号：
      最小化 / 被遮挡 / 渲染进程被挂起 / 系统唤醒期间都不受影响；
    - 两者都没了 → 才看心跳：收到过心跳的静默超过 ``HEARTBEAT_GRACE`` 退出，
      或页面主动发了「告别」（pagehide → sendBeacon）时立即退出；
    - 从未收到心跳：窗口进程**秒退**（存活 < 5s，首开竞争 / URL 转交）→ 补开
      （最多 ``MAX_REOPEN`` 次）；仍无则按 ``STARTUP_PATIENCE`` 放弃。
    """
    from backend import heartbeat

    opened_at = time.time()
    lived: float | None = None
    retries = 0
    probe: bool | None = None
    probe_at = 0.0
    last_probe: bool | None = None

    while True:
        if window is not None:
            try:
                window.wait(timeout=1.0)
                lived = time.time() - opened_at
                window = None
                if lived < 5.0:
                    print("[ZhiBan] 浏览器进程很快退出（可能转交给了已有实例），改用窗口/心跳判定")
                    _log_launcher("browser_process_exited_early", lived_s=round(lived, 2))
            except subprocess.TimeoutExpired:
                time.sleep(0.5)
                continue

        now = time.time()
        if now - probe_at >= PROBE_INTERVAL:
            probe_at = now
            probe = _app_window_alive()
            if probe != last_probe:
                _log_launcher("window_probe", alive=probe)
                last_probe = probe

        idle = heartbeat.idle_seconds()
        action = _lifetime_action(
            window_proc_alive=False, app_window_alive=probe, idle=idle,
            ever_seen=heartbeat.ever_seen(), lived=lived, retries=retries,
            bye=heartbeat.bye_seen(),
        )
        if action == "reopen":
            retries += 1
            print(f"[ZhiBan] 未检测到应用窗口与页面，尝试重新打开窗口（{retries}/{MAX_REOPEN}）")
            _log_launcher("reopen_window", attempt=retries)
            window = _open_window(port)
            if window is not None:
                opened_at = time.time()
                lived = None
                continue
        elif action == "exit-heartbeat":
            print("[ZhiBan] 应用窗口已消失且页面心跳停摆，准备退出")
            _log_launcher("exit", reason="window_gone_and_page_silent",
                          idle_s=round(idle, 1), probe=probe, bye=heartbeat.bye_seen())
            return
        elif action == "exit-startup":
            print("[ZhiBan] 页面迟迟未上线，退出（可重新打开知伴）")
            _log_launcher("exit", reason="startup_timeout", idle_s=round(idle, 1), probe=probe)
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
    _log_launcher("started", port=port, version=cfg.version)
    window = _open_window(port)
    _log_launcher("window_opened", browser_pid=(window.pid if window is not None else None))

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
    _log_launcher("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
