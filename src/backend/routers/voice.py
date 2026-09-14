"""语音路由（架构文档 §6.11）。

**红线**：``/api/asr/*`` 只做本地进程内推理，**永不发起外部网络请求**。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ..deps import get_settings_service
from ..errors import AppError, ok
from ..services.asr import ASRService
from ..services.tts import TTSService
from ..services import tts_providers

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
    meta: dict[str, Any] = {}
    data, content_type = TTSService.get_instance().speech(text, voice=voice, fmt=fmt, meta=meta)
    dropped = (meta.get("dropped") or []) + (meta.get("clipped") or [])
    headers = {}
    if dropped:
        # header 值只能 ASCII，用 quote 编码；前端 decode 后展示给用户。
        from urllib.parse import quote
        headers["X-TTS-Dropped"] = quote("；".join(str(d) for d in dropped))
    if meta.get("voice_sent"):
        from urllib.parse import quote
        headers["X-TTS-Voice"] = quote(str(meta["voice_sent"]))
    return Response(content=data, media_type=content_type, headers=headers)


class VoicesProbeRequest(BaseModel):
    """批量探测音色的请求体（不传 voices 就用 内置+缓存 的候选）。"""

    voices: list[str] | None = None
    limit: int = 12


@router.get("/tts/voices", summary="可用音色（官方接口 → 内置清单 → 探测缓存 三级合并）")
def tts_voices(refresh: int = Query(default=0, description="1=忽略探测缓存重新合并")) -> dict:
    """音色**不需要去官网逐个查**：三级来源合并后供设置页下拉选择。

    ① 官方接口（StepFun 实测返回 200 但列表为空）；② 内置清单（实测可用音色）；
    ③ 本地探测缓存。每项都带 ``source``，UI 会标注可信度。
    """
    s = get_settings_service()
    base_url, model, api_key = s.tts_effective()
    if not base_url or not model:
        raise AppError(4002, "云端朗读未配置",
                       "请先填写语音端点与模型名，再拉取音色")

    items: dict[str, dict[str, Any]] = {}
    counts = {"api": 0, "builtin": 0, "probe": 0}
    official_err: str | None = None

    official, official_err = tts_providers.fetch_official_voices(base_url, api_key)
    for v in official:
        items.setdefault(v["id"], {**v, "source": "api"})
    counts["api"] = len(official)

    for v in tts_providers.builtin_voices(base_url, model):
        cur = items.get(v["id"])
        if cur is None:
            items[v["id"]] = {**v, "source": "builtin"}
            counts["builtin"] += 1

    if not refresh:
        for v in tts_providers.load_probe_cache():
            cur = items.get(str(v["id"]))
            if cur is None:
                items[str(v["id"])] = {"id": str(v["id"]), "label": str(v.get("label") or v["id"]),
                                       "lang": "", "gender": "", "source": "probe"}
                counts["probe"] += 1

    cfg = tts_providers.effective_config(base_url, model)
    return ok({
        "provider": cfg["provider"],
        "provider_label": cfg["label"],
        "default_voice": cfg["default_voice"],
        "official_error": official_err,
        "source_counts": counts,
        "items": sorted(items.values(), key=lambda x: (x["source"] != "api", x["id"])),
    })


@router.post("/tts/voices/probe", summary="批量探测音色可用性（只回报告，不返回音频）")
def tts_voices_probe(payload: VoicesProbeRequest) -> dict:
    """逐个发最小合成请求，报告可用性。**会消耗少量额度**，由用户点击触发。"""
    s = get_settings_service()
    base_url, model, api_key = s.tts_effective()
    if not base_url or not model:
        raise AppError(4002, "云端朗读未配置",
                       "请先填写语音端点与模型名，再探测音色")

    builtin = [v["id"] for v in tts_providers.builtin_voices(base_url, model)]
    cached_list = tts_providers.load_probe_cache()
    cached = [str(v["id"]) for v in cached_list]
    cached_ok = {str(v["id"]) for v in cached_list}
    given = [str(v) for v in (payload.voices or []) if str(v).strip()]
    if given:
        # 显式指定：全部按用户给的测（即使探测过，也允许重测）
        candidates = given
    else:
        # 隐式：优先测「还没确认过可用」的；已确认的放最后，避免重复烧额度
        pool = list(dict.fromkeys(builtin + cached))
        rest = [x for x in pool if x not in cached_ok]
        candidates = rest + [x for x in pool if x in cached_ok]

    limit = max(1, min(int(payload.limit or 12), 24))
    todo, skipped = candidates[:limit], candidates[limit:]
    results = tts_providers.probe_voices(base_url, model, api_key, todo)

    good = [{"id": r["id"], "label": r["id"], "source": "probe"} for r in results if r["ok"]]
    if good:
        merged = {v["id"]: v for v in tts_providers.load_probe_cache()}
        merged.update({v["id"]: v for v in good})
        tts_providers.save_probe_cache(list(merged.values()))

    return ok({"items": results, "ok": len(good), "bad": len(results) - len(good),
               "cached": len(cached_ok), "already": len(cached_ok),
               "skipped": len(skipped)})


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
