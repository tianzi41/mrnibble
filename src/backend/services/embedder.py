"""嵌入服务：云端 ``/embeddings`` → 本地 :class:`LocalHashEmbedder` → 纯 FTS5。

降级链（架构 §12 / 主理人决策 #4）：
    1. **云端嵌入**：OpenAI 兼容 ``POST {base_url}/embeddings``（httpx 手写，不用 openai SDK）；
    2. **本地兜底** :class:`LocalHashEmbedder`：**纯 numpy 的字符 n-gram 哈希 + TF-IDF 权重**，
       维度 512，**零下载、零额外依赖、完全离线**；
    3. **纯 FTS5**：两条通道都不可用时不上报错误，检索退化为关键词召回。

约定：:meth:`Embedder.embed` 返回 ``(vectors, dim, model_name)``，向量已 L2 归一化。
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Sequence

import httpx

from ..utils.http import make_client
import numpy as np

from ..db.connection import get_db
from ..paths import resource_path
from ..utils.numpy_blob import l2_normalize

logger = logging.getLogger(__name__)

__all__ = ["Embedder", "LocalHashEmbedder", "LocalBgeEmbedder", "CloudEmbedder",
           "get_embedder"]

# 本地哈希嵌入维度（主理人决策 #4）。
LOCAL_HASH_DIM = 512

# CJK 与拉丁词切分。
_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD = re.compile(r"[a-z0-9]+")

# n-gram 长度先验（作为 IDF 的确定性近似：越长越具体，权重越高）。
_NGRAM_IDF_PRIOR: dict[str, float] = {"c1": 1.0, "c2": 1.7, "c3": 2.4, "w": 1.8}

# 云端嵌入请求超时（秒）。
_CLOUD_TIMEOUT = 30.0


def _hash64(token: str) -> int:
    """稳定哈希（跨进程一致，避免 Python 内置 hash 的随机化）。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little")


class LocalHashEmbedder:
    """本地哈希嵌入器（纯 numpy，字符 n-gram 哈希 + TF-IDF 权重，维度 512）。

    说明：
        - 对 CJK 文本抽取字符 1/2/3-gram，对拉丁文本抽取整词；
        - 采用 **特征哈希**（feature hashing）把 n-gram 映射到 512 维桶，并用符号位降低碰撞干扰；
        - 权重 = 亚线性 TF × IDF 先验（以 n-gram 长度近似 IDF），最后 L2 归一化；
        - 完全确定性、离线、零下载，用于云端嵌入不可用时的向量召回兜底。
    """

    name = "local-hash-512"

    def __init__(self, dim: int = LOCAL_HASH_DIM) -> None:
        self.dim = int(dim)

    def _ngrams(self, text: str) -> Counter:
        """抽取加权前的 n-gram 计数。"""
        counter: Counter = Counter()
        lowered = (text or "").lower()
        for word in _LATIN_WORD.findall(lowered):
            counter[f"w:{word}"] += 1
        cjk = "".join(ch for ch in lowered if _CJK_CHAR.match(ch))
        for n in (1, 2, 3):
            for i in range(len(cjk) - n + 1):
                counter[f"c{n}:{cjk[i:i + n]}"] += 1
        return counter

    def embed_one(self, text: str) -> np.ndarray:
        """把单段文本编码为归一化向量。"""
        vector = np.zeros(self.dim, dtype=np.float32)
        grams = self._ngrams(text)
        if not grams:
            return vector
        for token, tf in grams.items():
            prefix = token[:2]
            prior = _NGRAM_IDF_PRIOR.get(prefix, 1.5)
            weight = (1.0 + np.log(tf)) * prior
            h = _hash64(token)
            bucket = h % self.dim
            sign = 1.0 if (h >> 33) & 1 == 0 else -1.0
            vector[bucket] += sign * weight
        return l2_normalize(vector)

    def embed(self, texts: Sequence[str]) -> tuple[np.ndarray, int, str]:
        """批量编码。

        Args:
            texts: 文本序列。

        Returns:
            ``(vectors, dim, name)``，``vectors`` 形状 ``(n, dim)``。
        """
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32), self.dim, self.name
        matrix = np.vstack([self.embed_one(t) for t in texts]).astype(np.float32)
        return matrix, self.dim, self.name


class LocalBgeEmbedder:
    """本地 bge-small-zh-v1.5 语义嵌入器（ONNX INT8 量化，纯 onnxruntime + tokenizers）。

    说明：
        - 模型约 24MB（INT8），512 维，CPU 单条短文本几十毫秒，零外发、离线；
        - **CLS 池化 + L2 归一化**（与 FlagEmbedding 官方口径一致）；
        - :meth:`embed_query` 给查询加官方指令前缀（段落侧不加）——BGE 的
          「短查询 → 长段落」检索场景要求两侧口径不同；
        - 依赖（onnxruntime/tokenizers）或模型文件缺失时抛 RuntimeError，
          由 :class:`Embedder` 门面自动降级回 :class:`LocalHashEmbedder`。
    """

    name = "local-bge-512"
    # BAAI 官方为 bge-*-zh v1.5 推荐的查询指令前缀。
    QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

    def __init__(self, model_dir: str | Path) -> None:
        self.dir = Path(model_dir)
        self._session = None
        self._tokenizer = None

    def available(self) -> bool:
        """模型文件是否齐备（不加载、不抛异常）。"""
        return ((self.dir / "onnx" / "model_quantized.onnx").exists()
                and (self.dir / "tokenizer.json").exists())

    def _load(self) -> None:
        if self._session is not None:
            return
        if not self.available():
            raise RuntimeError(f"本地 bge 模型不完整：{self.dir}")
        # 延迟导入：关闭该功能（未装依赖）时不得影响其他功能。
        import onnxruntime  # noqa: PLC0415
        from tokenizers import Tokenizer  # noqa: PLC0415

        logger.info("加载本地 bge 语义嵌入模型",
                    extra={"extra_fields": {"dir": str(self.dir)}})
        tok = Tokenizer.from_file(str(self.dir / "tokenizer.json"))
        tok.enable_truncation(max_length=512)
        tok.enable_padding()   # 批内补齐到最长，否则 ids 无法组成矩形矩阵
        self._tokenizer = tok
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = 2   # 与 MeloTTS 一致：别抢满 CPU
        self._session = onnxruntime.InferenceSession(
            str(self.dir / "onnx" / "model_quantized.onnx"),
            sess_options=opts, providers=["CPUExecutionProvider"],
        )

    def _encode(self, texts: list[str], *, is_query: bool) -> np.ndarray:
        self._load()
        if is_query:
            texts = [self.QUERY_INSTRUCTION + t if t else t for t in texts]
        encs = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        att = np.array([e.attention_mask for e in encs], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": att}
        if any(i.name == "token_type_ids" for i in self._session.get_inputs()):
            feed["token_type_ids"] = np.zeros_like(ids)
        out = self._session.run(None, feed)[0]          # (n, seq, hidden)
        cls = np.ascontiguousarray(out[:, 0, :], dtype=np.float32)   # BGE 口径：取 [CLS]
        # 逐行 L2 归一化（utils.numpy_blob.l2_normalize 面向单向量会摊平矩阵，这里不能用它）
        norms = np.linalg.norm(cls, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return cls / norms

    def embed(self, texts: Sequence[str]) -> tuple[np.ndarray, int, str]:
        """批量编码段落文本（不加查询前缀）。"""
        if not texts:
            return np.zeros((0, 512), dtype=np.float32), 512, self.name
        return self._encode(list(texts), is_query=False), 512, self.name

    def embed_query_one(self, text: str) -> np.ndarray:
        """编码单条查询（带官方指令前缀）。"""
        return self._encode([text], is_query=True)[0]


class CloudEmbedder:
    """云端 OpenAI 兼容嵌入器（httpx 手写，``POST /embeddings``）。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "text-embedding-3-small"
        self.name = f"cloud:{self.model}"

    def embed(self, texts: Sequence[str]) -> tuple[np.ndarray, int, str]:
        """调用云端嵌入接口。

        Raises:
            RuntimeError: 端点未配置或调用失败（由上层降级处理）。
        """
        if not self.base_url:
            raise RuntimeError("云端嵌入端点未配置")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "input": list(texts)}
        with make_client(self.base_url, timeout=_CLOUD_TIMEOUT) as client:
            resp = client.post(f"{self.base_url}/embeddings", headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        items = body.get("data") or []
        if not items:
            raise RuntimeError("云端嵌入返回空结果")
        vectors = np.asarray([item["embedding"] for item in items], dtype=np.float32)
        # 逐行 L2 归一化，统一余弦口径。
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vectors = vectors / norms
        return vectors, int(vectors.shape[1]), self.name


class Embedder:
    """嵌入门面：按设置选择通道，并在失败时依次降级。

    降级链：云端 → :class:`LocalHashEmbedder` → （交由检索层退化为纯 FTS）。
    """

    def __init__(self) -> None:
        self._local = LocalHashEmbedder()
        self._bge: LocalBgeEmbedder | None = None   # 按需创建（选了 bge 引擎才加载）
        # 记录已成功的通道名，避免每批都重试已失败的云端。
        self._preferred: str | None = None

    # ── 配置读取 ────────────────────────────────────────
    def _read_config(self) -> dict[str, str]:
        """读取嵌入相关设置（直接查 settings 表，避免循环导入）。"""
        rows = get_db().query_all(
            "SELECT key, value FROM settings WHERE key IN "
            "('embed.provider','embed.base_url','embed.model','embed.api_key',"
            "'embed.local_engine','embed.local_dir',"
            "'llm.api_key','llm.base_url')"
        )
        raw = {r["key"]: (r["value"] or "") for r in rows}
        provider = raw.get("embed.provider", "auto") or "auto"
        # 与设置界面提示一致：embed.base_url 留空时沿用对话模型端点。
        base_url = (raw.get("embed.base_url") or raw.get("llm.base_url") or "").strip()
        return {
            "provider": provider,
            "base_url": base_url,
            "model": raw.get("embed.model", ""),
            "api_key": raw.get("embed.api_key", ""),
            "llm_api_key": raw.get("llm.api_key", ""),
            # 本地引擎：hash（默认，零下载）| bge（语义模型，需已下载）
            "local_engine": (raw.get("embed.local_engine") or "hash").strip().lower(),
            "local_dir": raw.get("embed.local_dir") or "models/embed/bge-small-zh-v1.5",
        }

    def _get_bge(self, cfg: dict[str, str]) -> LocalBgeEmbedder | None:
        """取（并缓存）本地 bge 嵌入器；模型不齐备返回 ``None``。"""
        if self._bge is not None:
            return self._bge
        d = Path(cfg.get("local_dir") or "")
        # 相对路径按随包资源解析（开发态=项目根，冻结态=_internal）；
        # 用户也可在设置里写绝对路径。
        resolved = d if d.is_absolute() else resource_path(*d.parts)
        bge = LocalBgeEmbedder(resolved)
        if not bge.available():
            return None
        self._bge = bge
        return bge

    def _cloud_configured(self, cfg: dict[str, str]) -> bool:
        """判断云端嵌入是否已配置（有 base_url 即可尝试）。"""
        return bool(cfg.get("base_url")) and cfg.get("provider") in ("cloud", "auto")

    def provider(self) -> str:
        """返回当前生效的通道名（用于状态展示与日志）。"""
        cfg = self._read_config()
        if self._preferred:
            return self._preferred
        if self._cloud_configured(cfg):
            return f"cloud:{cfg.get('model') or 'default'}"
        return self._local.name

    # ── 编码 ────────────────────────────────────────────
    def embed(self, texts: Sequence[str]) -> tuple[np.ndarray, int, str]:
        """按降级链编码文本。

        Args:
            texts: 文本序列。

        Returns:
            ``(vectors, dim, model_name)``。

        Raises:
            RuntimeError: 云端与本地通道均不可用（调用方应退化为纯 FTS）。
        """
        cfg = self._read_config()
        errors: list[str] = []

        # 通道 1：云端嵌入（若已配置且未被标记失败）。
        if self._cloud_configured(cfg) and self._preferred != self._local.name:
            api_key = cfg.get("api_key") or self._decrypt_llm_key(cfg)
            try:
                cloud = CloudEmbedder(cfg["base_url"], api_key, cfg["model"])
                vectors, dim, name = cloud.embed(texts)
                self._preferred = name
                return vectors, dim, name
            except Exception as exc:  # noqa: BLE001 - 降级到本地
                errors.append(f"cloud:{type(exc).__name__}")
                logger.warning("云端嵌入失败，降级本地哈希嵌入：%s", type(exc).__name__)

        # 通道 2：本地嵌入。bge（语义）为可选引擎且失败自动回退 hash（字符 n-gram）。
        local = self._local
        if cfg.get("local_engine") == "bge":
            bge = self._get_bge(cfg)
            if bge is not None:
                local = bge
            else:
                logger.warning("embed.local_engine=bge 但模型不完整，回退本地哈希嵌入",
                               extra={"extra_fields": {"dir": cfg.get("local_dir")}})
        try:
            vectors, dim, name = local.embed(texts)
            self._preferred = name
            return vectors, dim, name
        except Exception as exc:  # noqa: BLE001
            errors.append(f"local:{type(exc).__name__}")
            if local is not self._local:
                # bge 加载/推理异常（依赖损坏等）→ 立即回退哈希，不让检索挂掉。
                logger.warning("本地 bge 嵌入失败，回退本地哈希嵌入：%s", type(exc).__name__)
                try:
                    vectors, dim, name = self._local.embed(texts)
                    self._preferred = name
                    return vectors, dim, name
                except Exception as exc2:  # noqa: BLE001
                    errors.append(f"local-hash:{type(exc2).__name__}")

        raise RuntimeError("所有嵌入通道均不可用：" + ",".join(errors))

    def _decrypt_llm_key(self, cfg: dict[str, str]) -> str:
        """嵌入 Key 缺省时回退用 LLM Key（需解密）。"""
        token = cfg.get("llm_api_key") or ""
        if not token:
            return ""
        try:
            from ..security import get_security_manager

            return get_security_manager().decrypt(token)
        except Exception:  # noqa: BLE001
            return ""

    def embed_query(self, query: str) -> np.ndarray:
        """编码单条查询（失败返回空向量，检索层据此退化）。

        bge 通道下查询会加官方指令前缀（与段落侧口径不同）；
        云端/哈希通道两侧同构，无前缀差异。

        Args:
            query: 查询文本。

        Returns:
            归一化向量；不可用时为空数组。
        """
        try:
            cfg = self._read_config()
            # 仅在「本地 bge 是当前实际生效通道」时走带前缀的查询编码；
            # 云端可用时仍走云端（保持既有口径）。
            if not self._cloud_configured(cfg) and cfg.get("local_engine") == "bge":
                bge = self._get_bge(cfg)
                if bge is not None:
                    return bge.embed_query_one(query)
            vectors, _dim, _name = self.embed([query])
            if vectors.shape[0] == 0:
                return np.zeros(0, dtype=np.float32)
            return vectors[0]
        except Exception:  # noqa: BLE001
            return np.zeros(0, dtype=np.float32)


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """返回进程级 :class:`Embedder` 单例。"""
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder
