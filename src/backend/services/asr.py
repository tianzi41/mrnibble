"""本地语音识别 ASR（架构文档 §6.11 / §7.4，R-G01 / R-G02）。

**红线**：本模块**绝不发起任何网络请求**——模型文件本地加载，进程内推理。

实现要点（sherpa-onnx 1.13.8 实测验证，与网上多数教程不同）：
- ``read_wave`` 已被移除 → 用标准库 ``wave`` + ``numpy`` 自行解码 WAV；
- ``OfflineRecognizer(...)`` 构造函数**不接受参数** → 必须用
  ``OfflineRecognizer.from_sense_voice(...)`` 工厂方法；
- ``tokens`` 是 ``from_sense_voice`` 的参数（``cfg.model_config.sense_voice`` 上没有该字段）。
"""

from __future__ import annotations

import logging
import sys
import wave
from pathlib import Path

import numpy as np

from ..deps import get_settings_service
from ..errors import AppError
from ..paths import data_path, resource_path
from ..utils.timeutil import now_epoch_ms

logger = logging.getLogger(__name__)

__all__ = ["ASRService", "get_asr_service"]

# 目标采样率（sherpa-onnx 特征提取要求）。
_TARGET_SR = 16000
# 单次识别最长音频（秒），防止超大请求拖垮 CPU。
_MAX_SECONDS = 120


class ASRService:
    """本地 ASR（懒加载单例）。"""

    _instance: "ASRService | None" = None

    def __init__(self) -> None:
        self._recognizer = None
        self._loaded_model = ""

    @classmethod
    def get_instance(cls) -> "ASRService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 状态 ────────────────────────────────────────────
    def model_dir(self) -> Path:
        """模型目录（``asr.model_dir`` 设置项）。

        解析优先级（前一个存在即采用）：
        1. 设置项为绝对路径 → 直接使用；
        2. 冻结态：``exe 同级目录 / 设置项``（绿色包把 models/ 放在 exe 旁即可离线用）；
        3. 随包资源（``resource_path``，冻结态在 ``_internal/models``）；
        4. 运行数据目录（``data/models``）。
        """
        raw = get_settings_service().get("asr.model_dir").strip() or "models/asr/sense-voice-small"
        path = Path(raw)
        if path.is_absolute():
            return path

        def _has_model(d: Path) -> bool:
            return (d / "model.int8.onnx").exists() or (d / "model.onnx").exists()

        if getattr(sys, "frozen", False):
            beside = Path(sys.executable).resolve().parent / raw
            if _has_model(beside):
                return beside
        resolved = resource_path(*path.parts)
        if _has_model(resolved):
            return resolved
        return data_path("models", *path.parts)

    def available(self) -> bool:
        """模型文件是否就位（不含 ``tokens.txt`` 也可用 ``model.onnx``，这里要求两者）。"""
        d = self.model_dir()
        has_model = (d / "model.int8.onnx").exists() or (d / "model.onnx").exists()
        return has_model and (d / "tokens.txt").exists()

    def loaded(self) -> bool:
        """模型是否已在内存中。"""
        return self._recognizer is not None

    # ── 懒加载 ──────────────────────────────────────────
    def _load(self):
        """按需加载模型（首次约数秒，之后复用）。"""
        import sherpa_onnx

        d = self.model_dir()
        model = d / "model.int8.onnx"
        if not model.exists():
            model = d / "model.onnx"
        tokens = d / "tokens.txt"
        if not model.exists() or not tokens.exists():
            raise AppError(
                4000, "本地语音模型不可用",
                "未找到本地语音模型文件，请运行 scripts/download_models.ps1 下载后重试",
            )
        key = f"{model}|{tokens}"
        if self._recognizer is None or self._loaded_model != key:
            logger.info("加载本地 ASR 模型", extra={"extra_fields": {"model": model.name}})
            language = get_settings_service().get("asr.language").strip() or "zh"
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model),
                tokens=str(tokens),
                num_threads=2,
                use_itn=True,
                language=language,
                provider="cpu",
            )
            self._loaded_model = key
        return self._recognizer

    # ── 转写 ────────────────────────────────────────────
    def transcribe(self, wav_bytes: bytes) -> dict:
        """把 16k 单声道 WAV（兼容 8k/22.05k/44.1k/48k）转成文字。

        Args:
            wav_bytes: WAV 文件二进制。

        Returns:
            ``{"text": str, "duration_ms": int, "latency_ms": int}``

        Raises:
            AppError: 4000 模型不可用 / 1000 音频不合法 / 4001 识别失败。
        """
        started = now_epoch_ms()
        samples, sr = self._decode(wav_bytes)
        if samples.size == 0:
            raise AppError(1000, "音频为空")
        duration_ms = int(samples.size / sr * 1000)
        if duration_ms > _MAX_SECONDS * 1000:
            raise AppError(1000, None, f"单次录音最长 {_MAX_SECONDS} 秒")

        try:
            rec = self._load()
            stream = rec.create_stream()
            stream.accept_waveform(sr, samples)
            rec.decode_stream(stream)
            text = (stream.result.text or "").strip()
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 - 识别失败统一转业务错误
            logger.warning("ASR 识别失败", extra={"extra_fields": {"type": type(exc).__name__}})
            raise AppError(4001, "语音识别失败") from exc

        return {
            "text": text,
            "duration_ms": duration_ms,
            "latency_ms": int(now_epoch_ms() - started),
        }

    # ── 解码 ────────────────────────────────────────────
    @staticmethod
    def _decode(wav_bytes: bytes) -> tuple[np.ndarray, int]:
        """WAV → ``(float32 单声道, 采样率)``。

        支持 8/16/24/32 位 PCM；多声道取均值；非 16k 线性重采样。
        """
        import io

        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as w:
                n_ch = w.getnchannels()
                sampwidth = w.getsampwidth()
                sr = w.getframerate()
                n_frames = w.getnframes()
                raw = w.readframes(n_frames)
        except (wave.Error, EOFError) as exc:
            raise AppError(1000, None, f"不支持的音频格式：{type(exc).__name__}") from exc

        if n_frames == 0:
            return np.zeros(0, dtype=np.float32), sr

        if sampwidth == 2:
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif sampwidth == 1:
            samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sampwidth == 3:
            padded = raw + b"\x00" * (len(raw) % 3)
            arr = np.frombuffer(padded, dtype=np.uint8).reshape(-1, 3)
            ints = (
                arr[:, 0].astype(np.int32)
                | (arr[:, 1].astype(np.int32) << 8)
                | (arr[:, 2].astype(np.int32) << 16)
            )
            ints = np.where(ints >= 0x800000, ints - 0x1000000, ints)
            samples = ints.astype(np.float32) / 8388608.0
        elif sampwidth == 4:
            samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            raise AppError(1000, None, f"不支持的位深：{sampwidth * 8} bit")

        if n_ch > 1:
            samples = samples.reshape(-1, n_ch).mean(axis=1)

        if sr != _TARGET_SR and samples.size:
            target_len = int(round(samples.size * _TARGET_SR / sr))
            if target_len > 0:
                idx = np.linspace(0.0, samples.size - 1.0, target_len)
                samples = np.interp(idx, np.arange(samples.size), samples).astype(np.float32)
                sr = _TARGET_SR
        return samples.astype(np.float32), sr


def get_asr_service() -> ASRService:
    """返回 ASR 服务单例。"""
    return ASRService.get_instance()
