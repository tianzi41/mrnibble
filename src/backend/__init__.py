"""啃书先生（MrNibble）后端包。

本包承载「本机侧车服务（FastAPI）」，与桌面外壳（launcher）同进程运行。
所有路径经 :mod:`backend.paths` 获取，所有运行期配置经 :mod:`backend.config` 读取。
"""

from __future__ import annotations

__all__ = ["__version__"]

# 单一版本号来源，供 /api/health、/api/system/info 与打包脚本共用。
__version__ = "0.1.0"
