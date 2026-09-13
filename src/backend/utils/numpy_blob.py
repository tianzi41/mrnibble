"""向量 BLOB 编解码（架构文档 §14.8）。

约定：向量统一以 **float32 小端** 的 :meth:`numpy.ndarray.tobytes` 存 BLOB，
读取时用 ``numpy.frombuffer(blob, dtype='<f4')`` 还原。
"""

from __future__ import annotations

import numpy as np

__all__ = ["encode_vector", "decode_vector", "l2_normalize"]

# 统一存储端序：小端 float32（'<f4'）。
_DTYPE = np.dtype("<f4")


def encode_vector(vector: np.ndarray | list[float]) -> bytes:
    """把一维向量编码为 float32 小端 BLOB。

    Args:
        vector: 一维向量（numpy 数组或可转数组的序列）。

    Returns:
        float32 小端字节串。
    """
    arr = np.asarray(vector, dtype=_DTYPE).reshape(-1)
    return arr.tobytes()


def decode_vector(blob: bytes | memoryview | None) -> np.ndarray:
    """把 BLOB 解码为一维 float32 向量。

    Args:
        blob: ``encode_vector`` 产出的字节串；为 ``None`` 时返回空数组。

    Returns:
        一维 ``numpy.ndarray``（dtype ``<f4``）。
    """
    if not blob:
        return np.zeros(0, dtype=_DTYPE)
    raw = bytes(blob)
    return np.frombuffer(raw, dtype=_DTYPE).astype(np.float32, copy=True)


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    """对向量做 L2 归一化（零向量原样返回，避免除零）。

    Args:
        vector: 一维向量。

    Returns:
        归一化后的 float32 向量。
    """
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-12:
        return arr
    return (arr / norm).astype(np.float32, copy=False)
