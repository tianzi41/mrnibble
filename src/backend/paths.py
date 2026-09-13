"""资源与数据路径定位（全项目唯一的路径来源）。

本模块提供两个**语义分离**的函数，其它任何模块都**不得**用 ``__file__`` 自行拼装路径
（PyInstaller 冻结后 ``__file__`` 会指向临时解包目录，这是最常见的失败点）：

- :func:`resource_path` —— **只读**随包资源（``web/``、``models/``、``schema.sql``）。
- :func:`data_path` —— **可写**运行期数据（db / files / exports / logs / secret.key）。

冻结态与开发态的基址不同，因此 ``resource_path`` 采用「候选基址 + 命中即返回」策略：
按优先级依次尝试 ``_MEIPASS`` / ``_MEIPASS/src`` / 项目根 / 项目根 ``src``，
返回第一个真实存在的路径；若都不存在，则回退到首选基址（便于报错信息与占位创建）。

开发态目录布局（项目根 = ``src/backend/paths.py`` 的上两级）::

    zhiban/
    ├─ models/                 # 根目录
    ├─ data/                   # 运行期数据（自动生成）
    └─ src/
       ├─ web/                 # 静态前端
       └─ backend/db/schema.sql

冻结态（PyInstaller ``--onedir``）目录布局::

    dist/知伴/
    ├─ 知伴.exe
    ├─ _internal/              # = sys._MEIPASS
    │  ├─ web/
    │  ├─ models/
    │  └─ backend/db/schema.sql
    └─ data/                   # exe 同级，绿色便携
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "is_frozen",
    "project_root",
    "resource_path",
    "data_root",
    "data_path",
    "ensure_data_dirs",
]


def is_frozen() -> bool:
    """判断当前是否处于 PyInstaller 冻结态。

    同时识别 ``sys.frozen`` 与手动设置的 ``sys._MEIPASS``，
    以便测试可以用「设置 ``sys._MEIPASS``」的方式模拟冻结态。

    Returns:
        冻结态返回 ``True``，开发态返回 ``False``。
    """
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def project_root() -> Path:
    """返回开发态项目根目录（冻结态下不保证有意义，仅作回退）。

    Returns:
        项目根目录绝对路径（``src/backend/paths.py`` 的上两级）。
    """
    return Path(__file__).resolve().parents[2]


def _meipass() -> Path | None:
    """返回 PyInstaller 解包目录（``sys._MEIPASS``），不存在则返回 ``None``。"""
    value = getattr(sys, "_MEIPASS", None)
    return Path(value) if value else None


def _candidate_roots() -> list[Path]:
    """按优先级列出 ``resource_path`` 的候选基址（去重且保持顺序）。"""
    roots: list[Path] = []
    meipass = _meipass()
    if meipass is not None:
        roots.append(meipass)
        roots.append(meipass / "src")
    root = project_root()
    roots.append(root)
    roots.append(root / "src")

    seen: set[str] = set()
    unique: list[Path] = []
    for item in roots:
        key = str(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def resource_path(*parts: str) -> Path:
    """定位只读随包资源（``web/``、``models/``、``schema.sql`` 等）。

    Args:
        *parts: 相对资源根的路径片段，例如 ``("web", "index.html")``。

    Returns:
        第一个真实存在的候选绝对路径；都不存在时返回首选基址下的路径。
    """
    parts = tuple(str(p) for p in parts)
    candidates = [root.joinpath(*parts) for root in _candidate_roots()]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def data_root() -> Path:
    """返回运行期数据根目录（可写）。

    优先级：
        1. 环境变量 ``ZHIBAN_DATA_DIR``（绝对路径，覆盖一切）；
        2. 冻结态：``可执行文件同级目录 / data``（绿色便携）；
        3. 开发态：``项目根目录 / data``。

    Returns:
        数据根目录绝对路径。
    """
    override = os.environ.get("ZHIBAN_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent / "data"
    return project_root() / "data"


def data_path(*parts: str) -> Path:
    """定位运行期可写文件。

    Args:
        *parts: 相对数据根目录的路径片段，例如 ``("logs", "zhiban.log")``。

    Returns:
        数据根目录下的绝对路径（不保证已存在）。
    """
    root = data_root()
    return root.joinpath(*parts) if parts else root


def ensure_data_dirs() -> Path:
    """确保数据根目录及 ``files`` / ``exports`` / ``logs`` 子目录存在。

    Returns:
        数据根目录绝对路径。
    """
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("files", "exports", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
