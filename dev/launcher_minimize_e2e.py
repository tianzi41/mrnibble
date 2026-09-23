"""真机端到端复现：「挂后台 → 后台自己退出」事故。

⚠️ **会打开一个真实的浏览器应用窗口（约 2 分钟）**，请勿在正式使用时运行；
跑完会自动关掉那个窗口。标准回归清单里不含本脚本。

它在干什么：
    1. 用临时数据目录真起一次 ``src/launcher.py``（= 双击啃书先生.exe 的同一条路径）；
    2. 等到应用窗口出现，然后**把它最小化**（复现用户的操作）；
    3. 期间反复读 ``/api/health`` 的 ``heartbeat.idle_s``（心跳静默秒数）；
    4. 断言：启动器进程仍然活着、``launcher.log`` 里没有 exit 记录。

它能同时证两件事：
    ＊ **机制**：最小化后页面心跳是否真被浏览器拉长（``idle_s`` 会说明）；
    ＊ **修复**：即使心跳停摆，启动器也不再把服务停掉（窗口探测兜住）。

用法::

    .venv/Scripts/python.exe dev/launcher_minimize_e2e.py            # 默认守 100 秒
    .venv/Scripts/python.exe dev/launcher_minimize_e2e.py --hold 40  # 快跑一遍
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EXPECT_TITLE = "啃书先生 · 本地 AI 学习伴侣"
TMP = ROOT / ".tmp" / "e2e-launcher"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}    {detail}")


# ── win32 小工具 ─────────────────────────────────────────────
_U = ctypes.WinDLL("user32", use_last_error=True)


def _titles() -> list[tuple[int, str, int]]:
    """[(hwnd, title, pid)]，枚举全部顶层窗口。"""
    out: list[tuple[int, str, int]] = []
    cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        n = _U.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            _U.GetWindowTextW(hwnd, buf, n + 1)
            pid = wintypes.DWORD()
            _U.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append((int(hwnd), buf.value, int(pid.value)))
        return True

    _U.EnumWindows(cb_type(_cb), 0)
    return out


def _find_app_hwnd() -> int:
    for hwnd, title, _pid in _titles():
        if EXPECT_TITLE in title:
            return hwnd
    return 0


def _http_json(url: str, timeout: float = 3.0) -> dict:
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(url, timeout=timeout) as r:   # noqa: S310 - 本机回环
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=100.0, help="最小化后观察多少秒")
    ap.add_argument("--startup", type=float, default=75.0, help="等窗口出现的上限")
    args = ap.parse_args()

    import launcher

    print("[E2E] 挂后台复现：真起启动器 + 真最小化窗口")
    check("E0 启动器标记与页面标题字面量一致（不一致说明常量被改坏了）",
          launcher.APP_TITLE_MARK == EXPECT_TITLE, launcher.APP_TITLE_MARK)

    # 复用同一个数据目录，**不递归删除**：浏览器 profile 有几百个文件，
    # 递归清空会撞沙箱的批量删除护栏（SAFE_DELETE_BULK_CONFIRM_REQUIRED），
    # 脚本会在第 2 次运行时直接中断。只把 launcher.log 清掉（单文件）。
    TMP.mkdir(parents=True, exist_ok=True)
    (TMP / "logs").mkdir(parents=True, exist_ok=True)
    (TMP / "logs" / "launcher.log").unlink(missing_ok=True)
    env = dict(os.environ, MRNIBBLE_DATA_DIR=str(TMP))
    out_log = (TMP / "launcher-stdout.log").open("wb")

    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "src" / "launcher.py")],
                            cwd=str(ROOT), env=env,
                            stdout=out_log, stderr=subprocess.STDOUT)
    hwnd = 0
    port = 0
    try:
        # ① 等应用窗口出现
        t0 = time.time()
        while time.time() - t0 < args.startup:
            hwnd = _find_app_hwnd()
            if hwnd:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        check("E1 应用窗口已出现（启动器真起来了）", bool(hwnd),
              f"{time.time() - t0:.1f}s" if hwnd else "超时未出现")
        if not hwnd:
            return 1

        rt = json.loads((TMP / "runtime.json").read_text(encoding="utf-8"))
        port = rt["port"]
        health0 = _http_json(f"http://127.0.0.1:{port}/api/health")["data"]
        check("E2 /api/health 暴露心跳诊断字段", "heartbeat" in health0, str(health0.get("heartbeat")))
        check("E3 页面已开始心跳（ever_seen=True）",
              health0["heartbeat"]["ever_seen"] is True, str(health0["heartbeat"]))

        # ② 最小化（复现用户操作）
        _U.ShowWindow(hwnd, 1)      # SW_SHOWNORMAL
        _U.ShowWindow(hwnd, 6)      # SW_MINIMIZE
        t_min = time.time()
        check("E4 窗口已最小化", bool(_U.IsIconic(hwnd)), f"IsIconic={_U.IsIconic(hwnd)}")

        # ③ 观察：心跳静默会涨到多少？启动器会不会退出？
        max_idle = 0.0
        samples: list[float] = []
        while time.time() - t_min < args.hold:
            try:
                hb = _http_json(f"http://127.0.0.1:{port}/api/health", timeout=2)["data"]["heartbeat"]
                max_idle = max(max_idle, float(hb["idle_s"]))
                samples.append(float(hb["idle_s"]))
            except Exception as e:
                samples.append(-1.0)
                print(f"      （health 读不到：{type(e).__name__}——可能服务已停）")
            if proc.poll() is not None:
                print(f"      ⚠ 启动器进程在 {time.time() - t_min:.1f}s 时退出了")
                break
            time.sleep(5)

        alive = proc.poll() is None
        check("E5 ★ 最小化后启动器仍然活着（本次修复的核心）", alive,
              f"已守 {time.time() - t_min:.0f}s")
        # 这一条是**观测**不是断言：本机浏览器是否真的把心跳节流到 >15s，
        # 取决于版本/电源策略。峰值 >15s 表示「旧逻辑在此必然退出」，是机制实证。
        print(f"      E6 观测：最小化期间心跳静默峰值 {max_idle:.1f}s"
              f"（>15s = 旧逻辑必已退出；共 {len(samples)} 次采样）")
        logs = (TMP / "logs" / "launcher.log")
        txt = logs.read_text(encoding="utf-8") if logs.exists() else ""
        check("E7 launcher.log 里没有 exit 记录（没走过停机分支）",
              '"exit"' not in txt and "exit-heartbeat" not in txt,
              txt.strip().splitlines()[-1][:120] if txt.strip() else "（日志为空）")
        print(f"      采样（心跳静默秒数）：{[round(s, 1) for s in samples]}")

        # ④ 反向确认：真关窗后应当能停机（bye / 心跳停摆 → exit）
        _U.PostMessageW(hwnd, 0x0010, 0, 0)      # WM_CLOSE
        t_close = time.time()
        while time.time() - t_close < 30 and proc.poll() is None:
            time.sleep(0.5)
        check("E8 关掉窗口后启动器自己退出（正常路径没被破坏）",
              proc.poll() is not None, f"{time.time() - t_close:.1f}s")
        if logs.exists():
            tail = logs.read_text(encoding="utf-8").strip().splitlines()
            if tail:
                print("      launcher.log 末尾：" + tail[-1][:160])
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        try:
            hwnd2 = _find_app_hwnd()
            if hwnd2:
                _U.PostMessageW(hwnd2, 0x0010, 0, 0)
        except Exception:
            pass
        out_log.close()
        print(f"  （stdout/日志与临时数据留在 {TMP}）")

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  失败：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
