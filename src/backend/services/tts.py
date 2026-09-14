"""语音合成 TTS（架构文档 §6.11，R-G03~G05）。

**三态**：``off``（默认）/ ``local`` / ``cloud``（OpenAI 兼容 ``/audio/speech``）。

``local`` 有两种引擎（``tts.local_engine``）：

- ``system``（默认）：浏览器 ``speechSynthesis``，走 Windows 系统语音，
  **不经后端**，零依赖；缺点是音色机械、不同机器效果不一致；
- ``melo``：**本地 MeloTTS 神经语音**，后端用 sherpa-onnx 合成 WAV 返回前端播放。
  与本地 ASR 共用 sherpa-onnx，**不引入 PyTorch、不联网**，
  中文+英文混读（MIT + Apache-2.0，符合「只用宽松许可」约定）。

云端合成走用户自己填的端点；端点/Key 留空时沿用对话模型，**Key 的沿用仅限同一主机**
（把 A 站点的密钥发往 B 站点属密钥外泄，故异主机绝不沿用）；显式配置的 Key 仍只存本机（加密）。
"""

from __future__ import annotations

import io
import logging
import sys
import time
import wave
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from ..utils.http import make_client

from ..deps import get_settings_service
from ..errors import AppError
from ..paths import data_path, resource_path
from .settings_service import upstream_hint, _looks_like_voice_problem
from .tts_providers import default_voice, model_caps, provider_key

logger = logging.getLogger(__name__)

__all__ = ["TTSService", "get_tts_service"]

# 云端合成超时（朗读文本可能较长，读取放宽）。
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 120.0

# 本地合成单段文本上限（MeloTTS 是逐句模型，过长会拖慢并占用内存）。
_MAX_LOCAL_CHARS = 300
# 本地合成默认语速。
_DEFAULT_SPEED = 1.0


class TTSService:
    """TTS 状态与云端合成代理（进程级单例）。"""

    _instance: "TTSService | None" = None

    def __init__(self) -> None:
        # 本地 MeloTTS 引擎（懒加载，只有真正合成时才载入模型）。
        self._tts = None
        self._loaded_model = ""

    @classmethod
    def get_instance(cls) -> "TTSService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 状态 ────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """``GET /api/tts/status`` 数据。

        说明:
            ``local_available`` 恒为 ``True``——``system`` 引擎由浏览器
            ``speechSynthesis`` 提供（Windows 自带 SAPI 音色），不依赖后端；
            ``local_model_available`` 才表示 MeloTTS 模型是否就位。
        """
        s = get_settings_service()
        mode = (s.get("tts.mode") or "off").strip().lower()
        enabled = s.get_bool("tts.enabled")
        cloud_configured = bool(s.get("tts.base_url") and s.get("tts.model"))
        engine = (s.get("tts.local_engine") or "system").strip().lower()
        if engine not in ("system", "melo"):
            engine = "system"
        return {
            "enabled": enabled and mode != "off",
            "mode": mode if mode in ("off", "local", "cloud") else "off",
            "cloud_configured": cloud_configured,
            "voice": s.get("tts.voice"),
            "local_available": True,
            "local_engine": engine,
            "local_model_available": self.local_available(),
            "local_loaded": self._tts is not None,
        }

    # ── 本地 MeloTTS ────────────────────────────────────
    def model_dir(self) -> Path:
        """MeloTTS 模型目录（``tts.local_dir``）。

        解析优先级（与 ASR 一致）：绝对路径 → 冻结态 exe 同级 → 随包资源
        → 运行数据目录。这样绿色包把 models/ 放 exe 旁即可离线用。
        """
        raw = get_settings_service().get("tts.local_dir").strip() or "models/tts/melo-zh_en"
        path = Path(raw)
        if path.is_absolute():
            return path

        def _has(d: Path) -> bool:
            return (d / "model.onnx").exists()

        if getattr(sys, "frozen", False):
            beside = Path(sys.executable).resolve().parent / raw
            if _has(beside):
                return beside
        resolved = resource_path(*path.parts)
        if _has(resolved):
            return resolved
        return data_path(*path.parts)

    def local_available(self) -> bool:
        """MeloTTS 模型文件是否就位。"""
        d = self.model_dir()
        return all((d / name).exists() for name in ("model.onnx", "tokens.txt", "lexicon.txt"))

    def _load_local(self):
        """懒加载 MeloTTS（首次约数秒，之后复用）。"""
        import sherpa_onnx

        d = self.model_dir()
        model = d / "model.onnx"
        tokens = d / "tokens.txt"
        lexicon = d / "lexicon.txt"
        if not self.local_available() or not lexicon.exists():
            raise AppError(
                4000, "本地语音模型不可用",
                "未找到 MeloTTS 模型，请运行 scripts/download_tts_model.py 下载后重试",
            )
        key = str(model)
        if self._tts is not None and self._loaded_model == key:
            return self._tts

        logger.info("加载本地 MeloTTS 模型", extra={"extra_fields": {"dir": str(d)}})
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=str(model),
            tokens=str(tokens),
            # MeloTTS 的英文发音依赖 lexicon.txt，不能省略，否则中英混读会退化。
            lexicon=str(lexicon),
            data_dir="",
            dict_dir=str(d / "dict") if (d / "dict").exists() else "",
        )
        # Windows 中文路径下，kaldifst 对逗号分隔的绝对 FST 路径存在兼容性问题：
        # model/lexicon 可以正常打开，但 date.fst 会被底层报「无法打开」。
        # FST 只是日期/数字规范化增强，不影响中英基本发音，因此先关闭，
        # 以保证绿色包在含中文路径的任意设备上都能稳定朗读。
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(vits=vits, num_threads=2, provider="cpu"),
            rule_fsts="",
            max_num_sentences=1,
        )
        self._tts = sherpa_onnx.OfflineTts(cfg)
        self._loaded_model = key
        return self._tts

    def synth_local(self, text: str, *, speed: float = _DEFAULT_SPEED,
                    speaker: int = 0) -> tuple[bytes, int]:
        """本地合成一段文本，返回 ``(wav 字节, 采样率)``。

        Raises:
            AppError: 4000 模型不可用 / 4003 合成失败。
        """
        text = (text or "").strip()
        if not text:
            raise AppError(1000, "朗读内容为空")
        if len(text) > _MAX_LOCAL_CHARS:
            text = text[:_MAX_LOCAL_CHARS]
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            speed = _DEFAULT_SPEED
        speed = max(0.5, min(2.0, speed))

        tts = self._load_local()
        started = time.perf_counter()
        try:
            audio = tts.generate(text, sid=int(speaker or 0), speed=speed)
        except Exception as exc:
            logger.warning("本地合成失败", extra={"extra_fields": {"type": type(exc).__name__}})
            raise AppError(4003, None, f"本地合成失败（{type(exc).__name__}）") from exc

        samples = np.asarray(audio.samples, dtype=np.float32)
        if samples.size == 0:
            raise AppError(4003, "本地合成返回空音频")
        pcm = np.clip(samples, -1.0, 1.0)
        pcm = (pcm * 32767).astype("<i2")

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(audio.sample_rate))
            wf.writeframes(pcm.tobytes())
        logger.info("本地 TTS 合成完成",
                    extra={"extra_fields": {"ms": int((time.perf_counter() - started) * 1000),
                                            "chars": len(text)}})
        return buf.getvalue(), int(audio.sample_rate)

    # ── 云端合成 ────────────────────────────────────────
    def speech(self, text: str, *, voice: str | None = None, fmt: str = "mp3",
               meta: dict[str, Any] | None = None) -> tuple[bytes, str]:
        """调用云端 ``/audio/speech`` 返回音频字节。

        Args:
            meta: 可选出参。传入 dict 时会被填入 ``dropped``（被忽略的参数）、
                ``clipped``（被截断的字段）与 ``voice_sent``（实际发送的音色），
                供路由层回显给用户——**静默改参数是最难查的问题，必须可见**。

        Raises:
            AppError: 4002 未配置 / 4003 合成失败。
        """
        s = get_settings_service()
        if (s.get("tts.mode") or "off") != "cloud":
            raise AppError(4002, None, "当前未启用云端朗读（tts.mode != cloud）")
        base_url, model, api_key = s.tts_effective()
        if not base_url or not model:
            raise AppError(4002, "云端朗读未配置",
                           "请在语音设置里填写语音端点与模型名（端点与对话模型同站点时可留空）")

        text = (text or "").strip()
        if not text:
            raise AppError(1000, "朗读内容为空")

        # 能力矩阵：发送前裁剪（docs/07 §4）——不支持的参数不发，超长文本截断，
        # 全部记录进 meta 回显，避免「静默改了参数」这种最难查的问题。
        caps = model_caps(base_url, model)
        dropped: list[str] = []
        clipped: list[str] = []
        max_chars = int(caps.get("max_chars") or 4000)
        if len(text) > max_chars:
            text = text[:max_chars]
            clipped.append(f"text（>{max_chars} 字，已截断）")
        allowed_fmts = caps.get("formats") or ["mp3"]
        if fmt not in allowed_fmts:
            dropped.append(f"response_format={fmt}（该模型不支持）")
            fmt = "mp3" if "mp3" in allowed_fmts else allowed_fmts[0]

        url = f"{base_url}/audio/speech"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        voice_sent = (voice or s.get("tts.voice") or default_voice(base_url, model)).strip()
        body = {
            "model": model,
            "input": text,
            "voice": voice_sent,
            "response_format": fmt,
        }
        if meta is not None:
            meta.update({"dropped": dropped, "clipped": clipped, "voice_sent": voice_sent,
                         "provider": provider_key(base_url), "max_chars": max_chars})

        started = time.perf_counter()
        try:
            with make_client(url, timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT,
                                      write=30.0, pool=10.0)) as client:
                resp = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("TTS 请求失败", extra={"extra_fields": {"type": type(exc).__name__}})
            raise AppError(4003, None, f"无法连接语音端点（{type(exc).__name__}）") from exc

        if resp.status_code >= 400:
            hint = upstream_hint(resp)
            detail = f"语音端点返回 HTTP {resp.status_code}"
            if hint:
                detail += f"：{hint}"
            if _looks_like_voice_problem(hint):
                detail += ("。该服务商可能不支持当前音色——请在「设置 → 语音 → 音色」填写"
                           "该服务商自己的音色名（例如 StepFun 用 cixingnansheng）")
            raise AppError(4003, None, detail)
        content_type = resp.headers.get("content-type", "audio/mpeg")
        data = resp.content
        if not data:
            raise AppError(4003, "语音合成返回空内容")
        logger.info("TTS 合成完成",
                    extra={"extra_fields": {"ms": int((time.perf_counter() - started) * 1000),
                                            "bytes": len(data)}})
        return data, content_type


def get_tts_service() -> TTSService:
    """返回 TTS 服务单例。"""
    return TTSService.get_instance()
