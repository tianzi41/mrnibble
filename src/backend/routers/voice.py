"""语音路由（架构文档 §6.11）。

**红线**：``/api/asr/*`` 只做本地进程内推理，**永不发起外部网络请求**。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import Response

from ..errors import AppError, ok
from ..services.asr import ASRService
from ..services.tts import TTSService

router = APIRouter()

__all__ = ["router"]

_MAX_AUDIO_BYTES = 30 * 1024 * 1024


# ── ASR（本地）─────────────────────────────────────────
@router.get("/asr/status")
def asr_status() -> dict:
    """本地语音识别状态。"""
    svc = ASRService.get_instance()
    return ok({
        "available": svc.available(),
        "model": "sense-voice-small",
        "loaded": svc.loaded(),
    })


@router.post("/asr/transcribe")
async def asr_transcribe(audio: UploadFile = File(description="16k 单声道 WAV")) -> dict:
    """本地语音识别：录音 → 文字（无任何外发请求）。"""
    data = await audio.read()
    if not data:
        raise AppError(1000, "音频内容为空")
    if len(data) > _MAX_AUDIO_BYTES:
        raise AppError(3002, "音频过大")
    result = ASRService.get_instance().transcribe(data)
    return ok(result)


# ── TTS（可选）─────────────────────────────────────────
@router.get("/tts/status")
def tts_status() -> dict:
    """朗读状态（默认关闭，R-G03）。"""
    return ok(TTSService.get_instance().status())


@router.post("/tts/speech")
def tts_speech(payload: dict) -> Response:
    """云端朗读合成（``mode=cloud`` 时可用；本地朗读由前端浏览器提供）。"""
    text = str(payload.get("text") or "")
    voice = payload.get("voice") or None
    fmt = str(payload.get("format") or "mp3")
    data, content_type = TTSService.get_instance().speech(text, voice=voice, fmt=fmt)
    return Response(content=data, media_type=content_type)


@router.post("/tts/local")
def tts_local(payload: dict) -> Response:
    """本地 MeloTTS 合成：文本 → WAV（**进程内推理，零外发**）。

    与 ``/api/asr/transcribe`` 一样走 sherpa-onnx，只是方向相反。
    前端在 ``tts.local_engine=melo`` 时按句调用本端点，逐段播放。
    """
    text = str(payload.get("text") or "")
    speed = payload.get("speed") or 1.0
    try:
        speaker = int(payload.get("speaker") or 0)
    except (TypeError, ValueError):
        speaker = 0
    data, sample_rate = TTSService.get_instance().synth_local(
        text, speed=float(speed), speaker=speaker)
    return Response(content=data, media_type="audio/wav",
                    headers={"X-Sample-Rate": str(sample_rate)})
