"""服务层包：解析 / 切片 / 嵌入 / 检索 / 问答 / 引导 / 记忆 / 生成 / 导出 / 语音 / 设置。

本包 ``__init__`` 保持轻量，具体服务在各模块内定义并按需惰性导入，
以避免启动期加载重量级依赖（jieba / sherpa-onnx 等）。
"""

from __future__ import annotations

__all__: list[str] = []
