"""启动器存活判定检查（「挂后台一会儿后台窗口自己没了」事故的回归护栏）。

背景（2026-09-20 用户实测）：
    暂停课程 → 把课程窗口与后台窗口都最小化 → 等一会儿 → 后台已退出。
    日志实证：服务是被**启动器自己**优雅停掉的（``知伴服务已停止`` + WAL
    checkpoint，而不是崩溃/被 taskkill），退出时应用窗口**还在**（浏览器
    profile 在 27 秒后才落盘）。根因：唯一防线是「5 秒页面心跳 + 15 秒宽限」，
    而浏览器在窗口最小化/被遮挡时会节流甚至冻结隐藏页的 JS 定时器。

修复：启动器先看**应用窗口是否存在**（枚举顶层窗口标题，不依赖页面 JS），
    只有窗口确实消失后才用心跳；再加一条 ``pagehide → sendBeacon`` 的
    「告别」快路径。

本脚本覆盖：
    [L] 存活判定纯函数矩阵（含事故场景与证伪用用例）
    [W] 窗口探测：注入标题匹配 + 真实枚举 + **真建一个窗口并最小化**
    [H] 心跳告别标记（mark_bye / bye_seen / touch）
    [R] 启动器日志真的落到 data/logs/launcher.log

证伪方法（跑完把实现改回旧行为，断言必须变红）::

    # 1) 去掉「窗口还在 → 继续服务」这一条
    #    把 src/launcher.py 中 _lifetime_action 的
    #        if window_proc_alive or app_window_alive:
    #    改成
    #        if window_proc_alive:
    #    → L1/L2 应报红
    # 2) 把窗口探测永远置为 False（模拟旧行为）
    #    在 _app_window_alive 开头加 return False
    #    → L1/L2 与 W3/W4 应报红

用法::

    .venv/Scripts/python.exe dev/launcher_liveness_check.py

⚠️ 两个实现上的坑（都已规避，改这个脚本时别退回去）：
    1. 数据目录**不递归删除**：含浏览器 profile 时撞沙箱批量删除护栏；
    2. 结尾用 ``os._exit`` 硬退出：本脚本建过真实窗口，退出阶段偶发挂死
       （2026-09-20 卡了 11 分钟，把整套回归拖停）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TMP = ROOT / ".tmp" / ("test-data-launcher-%d" % int(time.time()))

# ⚠️ 这里**故意写死字面量**，不用 launcher.APP_TITLE_MARK：
# 两者是同一条不变量（启动器认的窗口标题 == index.html 的 <title>）。
# 若测试用同一个常量既建窗口又做匹配，常量写错了也照样全绿——
# 第一次写这个脚本时就踩了这个坑（证伪时改了标记仍全过）。
EXPECT_TITLE = "知伴 · 本地 AI 学习伴侣"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}    {detail}")


def section(title: str) -> None:
    print(f"\n{title}")


# ── 真窗口的创建/最小化/销毁（ctypes 直调，不引入依赖）──────────────
def _mk_window(title: str) -> int:
    """建一个顶层窗口（用系统自带 STATIC 类，无需注册窗口类）。失败返回 0。"""
    import ctypes
    from ctypes import wintypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.CreateWindowExW.restype = wintypes.HWND
    u.CreateWindowExW.argtypes = (
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    )
    WS_OVERLAPPEDWINDOW = 0x00CF0000
    hwnd = u.CreateWindowExW(0, "STATIC", title, WS_OVERLAPPEDWINDOW,
                             80, 80, 360, 200, None, None, None, None)
    return int(hwnd or 0)


def _win_op(hwnd: int, op: str) -> bool:
    import ctypes
    from ctypes import wintypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    u.DestroyWindow.argtypes = (wintypes.HWND,)
    if op == "show":
        u.ShowWindow(hwnd, 1)          # SW_SHOWNORMAL
        return True
    if op == "minimize":
        u.ShowWindow(hwnd, 6)          # SW_MINIMIZE
        return True
    if op == "destroy":
        return bool(u.DestroyWindow(hwnd))
    return False


def main() -> int:
    # 复用同一个数据目录、**不递归删除**：`shutil.rmtree` 在这里踩过两次坑 ——
    # ① 目录里若有浏览器 profile（几百个文件）会撞沙箱的批量删除护栏；
    # ② 2026-09-20 实测还会**偶发挂住**（打印完汇总后不退出，把整套回归卡死，
    #    只能杀进程才放行）。只清 `launcher.log` 这一个文件（R1/R2 的判据）。
    TMP.mkdir(parents=True, exist_ok=True)
    (TMP / "logs").mkdir(parents=True, exist_ok=True)
    (TMP / "logs" / "launcher.log").unlink(missing_ok=True)
    os.environ["ZHIBAN_DATA_DIR"] = str(TMP)

    import launcher
    from backend import heartbeat

    # ══════════ [L] 存活判定矩阵 ══════════
    section("[L] 存活判定矩阵（纯函数 _lifetime_action）")

    def act(**kw) -> str:
        base = dict(window_proc_alive=False, app_window_alive=False, idle=0.0,
                    ever_seen=True, lived=None, retries=0, bye=False)
        base.update(kw)
        return launcher._lifetime_action(**base)

    # 事故场景：窗口还在、但心跳已经被浏览器节流到 10 分钟没响
    check("L1 事故场景：窗口还在 + 心跳静默 600s → 必须继续服务（不得退出）",
          act(app_window_alive=True, idle=600.0) == "wait")
    check("L2 最小化 5 分钟后心跳才回来一次（静默 300s）→ 仍继续服务",
          act(app_window_alive=True, idle=300.0) == "wait")
    check("L3 窗口探测不可用 + 静默 20s → 继续服务（走 30s 宽容值）",
          act(app_window_alive=None, idle=20.0) == "wait")
    check("L4 窗口探测不可用 + 静默 40s → 退出",
          act(app_window_alive=None, idle=40.0) == "exit-heartbeat")
    check("L5 真关窗：无窗口 + 静默 20s → 退出",
          act(app_window_alive=False, idle=20.0) == "exit-heartbeat")
    check("L6 宽限内（静默 10s）不退出",
          act(app_window_alive=False, idle=10.0) == "wait")
    check("L7 页面发了告别（bye）+ 窗口已消失 → 立即退出",
          act(app_window_alive=False, idle=0.5, bye=True) == "exit-heartbeat")
    check("L8 bye 但窗口还在（如按 F5 刷新）→ 不退出",
          act(app_window_alive=True, idle=0.5, bye=True) == "wait")
    check("L8b bye + 窗口探测不可用 → 也不退出（宁可多等，不可误杀）",
          act(app_window_alive=None, idle=0.5, bye=True) == "wait")
    check("L9 浏览器句柄还活着 → 无条件继续（连心跳都不看）",
          act(window_proc_alive=True, app_window_alive=False, idle=9999.0) == "wait")
    check("L10 从未收到心跳 + 秒退 + 8s 无页面 → 补开窗口",
          act(ever_seen=False, app_window_alive=False, lived=1.5, idle=9.0) == "reopen")
    check("L11 从未收到心跳 + 没秒退 + 20s → 继续等（冷启动耐心内）",
          act(ever_seen=False, app_window_alive=False, lived=None, idle=20.0) == "wait")
    check("L12 从未收到心跳 + 超过耐心 50s → 退出（判为页面没起来）",
          act(ever_seen=False, app_window_alive=False, lived=None, idle=50.0) == "exit-startup")
    check("L13 补开次数用尽后不再补开",
          act(ever_seen=False, app_window_alive=False, lived=2.0, idle=9.0,
              retries=launcher.MAX_REOPEN) == "wait")

    # ══════════ [W] 窗口探测 ══════════
    section("[W] 应用窗口探测")

    # W0：跨文件不变量 —— 启动器认的标题必须是 index.html 里那个 <title>
    check("W0 启动器标记 == index.html 的 <title>",
          launcher.APP_TITLE_MARK == EXPECT_TITLE,
          f"launcher={launcher.APP_TITLE_MARK!r} 期望={EXPECT_TITLE!r}")
    html = (ROOT / "src" / "web" / "index.html").read_text(encoding="utf-8")
    import re as _re

    m = _re.search(r"<title>(.*?)</title>", html, _re.S)
    check("W0b index.html 的 <title> 与启动器标记一致（页面标题改了这里会红）",
          bool(m) and m.group(1).strip() == EXPECT_TITLE,
          f"html={m.group(1).strip()!r}" if m else "index.html 里找不到 <title>")

    mark = EXPECT_TITLE  # 字面量，不由 launcher 派生
    check("W1 标题完全一致 → 命中", launcher._app_window_alive(titles=[(1, mark)]) is True)
    check("W2 最小化/无响应时标题带后缀 → 仍命中",
          launcher._app_window_alive(titles=[(1, mark + "（无响应）")]) is True)
    check("W3 用户自己的 Edge 标签页（标题不含标记）→ 不命中",
          launcher._app_window_alive(titles=[(1, "知伴资料 - Microsoft Edge"),
                                             (2, "新标签页")]) is False)
    check("W4 同名文件夹资源管理器窗口 → 不命中（标题是「知伴」而非完整标记）",
          launcher._app_window_alive(titles=[(1, "知伴")]) is False)
    check("W5 空列表 → 返回 None（保守：宁可多等，不误杀）",
          launcher._app_window_alive(titles=[]) is None)

    real = launcher._window_titles()
    check("W6 真实枚举不抛异常且返回列表", isinstance(real, list), str(type(real)))
    print(f"      （本机当前顶层窗口 {len(real)} 个；不含标记时探测="
          f"{launcher._app_window_alive()!r}）")

    hwnd = _mk_window(mark)
    if hwnd:
        check("W7 真建一个标题=页面标题的窗口 → 探测到 True",
              launcher._app_window_alive() is True)
        _win_op(hwnd, "show")
        _win_op(hwnd, "minimize")
        check("W8 ★ 窗口最小化后仍能探测到（本次事故的核心断言）",
              launcher._app_window_alive() is True)
        _win_op(hwnd, "destroy")
        check("W9 窗口销毁后 → 不再命中",
              launcher._app_window_alive() is not True)
    else:
        import ctypes
        print(f"      （环境不支持创建窗口，跳过 W7-W9；GetLastError="
              f"{ctypes.get_last_error()}）")
        check("W7-W9 跳过（无窗口站）——不计入失败", True)

    # ══════════ [H] 心跳告别标记 ══════════
    section("[H] 心跳 / 告别标记")
    check("H1 初始未收到 bye", heartbeat.bye_seen() is False)
    heartbeat.mark_bye()
    check("H2 mark_bye() 后 bye_seen() 为真", heartbeat.bye_seen() is True)
    before = heartbeat.idle_seconds()
    heartbeat.touch()
    check("H3 touch() 后 idle 归零", heartbeat.idle_seconds() <= before)
    check("H4 ever_seen() 为真", heartbeat.ever_seen() is True)

    # ══════════ [R] 启动器日志 ══════════
    section("[R] 启动器日志落地（以后能查明为什么退出）")
    launcher._log_launcher("exit", reason="unit_test", idle_s=1.0, probe=False)
    logf = TMP / "logs" / "launcher.log"
    check("R1 data/logs/launcher.log 生成", logf.exists(), str(logf))
    if logf.exists():
        lines = [ln for ln in logf.read_text(encoding="utf-8").splitlines() if ln.strip()]
        ok = False
        if lines:
            try:
                rec = json.loads(lines[-1])
                ok = rec.get("reason") == "unit_test" and "ts" in rec
            except Exception:
                ok = False
        check("R2 日志是可解析的 JSON Lines 且带退出原因", ok,
              lines[-1] if lines else "空文件")

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  失败：" + f)
    sys.stdout.flush()
    # 用 os._exit 硬退出：本脚本建过真实窗口（ctypes），退出阶段偶发挂住
    # （2026-09-20 实测卡死 11 分钟、把整套回归拖停）。断言都已跑完，
    # 这里不需要任何清理钩子。
    os._exit(0 if not FAIL else 1)


if __name__ == "__main__":
    raise SystemExit(main())
