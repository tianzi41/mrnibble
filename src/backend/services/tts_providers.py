"""云端 TTS 的「提供方」元数据：能力矩阵 + 内置音色清单 + 主机识别。

为什么需要这一层（docs/07 §0）：`voice` 不是抽象概念，而是**平台/模型命名空间里的
ID**，天生不通用——`alloy` 只有 OpenAI 系认，StepFun 会直接 400
（`The voice_id (alloy) does not exist`）。所以只填「地址 + 模型名」跨不了平台。

本模块只负责**静态元数据**：

- 主机识别（按 base_url 的 netloc 选 provider，`*` 兜底 generic）；
- 能力矩阵（`max_chars` / `formats` …，用于**发送前裁剪参数**，避免 400）；
- 内置音色清单（官方 `/audio/voices` 返回空时的兜底，例如 StepFun 实测就是空）；
- 默认音色（`voice` 留空时用它，**不再写死 `alloy`**）。

「拉官方音色 / 批量探测」这类需要联网的发现动作在 `tts.py` 与 `routers/voice.py`。

用户可覆盖：`data/tts_providers.json`（结构同 `BUILTIN`，按 provider→models 逐层覆盖），
改完**不必改代码**——这是「长尾平台」的出路。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..paths import data_path
from ..utils.http import make_client

__all__ = [
    "BUILTIN",
    "provider_key",
    "model_caps",
    "builtin_voices",
    "default_voice",
    "effective_config",
]

# 实测口径（2026-09-14，StepFun stepaudio-2.5-tts）：
#   cixingnansheng 等 8 个 → 200；alloy/echo/nova/shimmer → 400 voice_id_invalid
BUILTIN: dict[str, dict[str, Any]] = {
    "stepfun": {
        "match_hosts": ("api.stepfun.com",),
        "label": "阶跃星辰 StepFun",
        "models": {
            "*": {
                "default_voice": "cixingnansheng",
                "voices": [
                    {"id": "cixingnansheng", "label": "磁性男声", "lang": "zh", "gender": "male"},
                    {"id": "shenchennanyin", "label": "深沉男音", "lang": "zh", "gender": "male"},
                    {"id": "qingniandaxuesheng", "label": "青年大学生", "lang": "zh", "gender": "male"},
                    {"id": "qinqienvsheng", "label": "亲切女声", "lang": "zh", "gender": "female"},
                    {"id": "wenrounvsheng", "label": "温柔女声", "lang": "zh", "gender": "female"},
                    {"id": "ruanmengnvsheng", "label": "软萌女声", "lang": "zh", "gender": "female"},
                    {"id": "jilingshaonv", "label": "机灵少女", "lang": "zh", "gender": "female"},
                    {"id": "yuanqishaonv", "label": "元气少女", "lang": "zh", "gender": "female"},
                ],
                "caps": {"max_chars": 1000, "formats": ["mp3", "wav", "pcm"]},
            },
        },
    },
    "openai": {
        "match_hosts": ("api.openai.com",),
        "label": "OpenAI",
        "models": {
            "*": {
                "default_voice": "alloy",
                "voices": [
                    {"id": "alloy", "label": "Alloy（中性）"},
                    {"id": "echo", "label": "Echo"},
                    {"id": "fable", "label": "Fable"},
                    {"id": "onyx", "label": "Onyx（男声）"},
                    {"id": "nova", "label": "Nova（女声）"},
                    {"id": "shimmer", "label": "Shimmer（女声）"},
                ],
                "caps": {"max_chars": 4096,
                         "formats": ["mp3", "opus", "aac", "flac", "wav", "pcm"]},
            },
        },
    },
    # 兜底：识别不出的站点按「OpenAI 兼容基础子集」对待
    "generic": {
        "match_hosts": ("*",),
        "label": "通用（OpenAI 兼容）",
        "models": {
            "*": {
                "default_voice": "alloy",
                "voices": [],
                "caps": {"max_chars": 4000, "formats": ["mp3", "wav"]},
            },
        },
    },
}


def _host(base_url: str) -> str:
    try:
        return (urlsplit(base_url).netloc or "").lower().split(":")[0]
    except ValueError:
        return ""


def _user_overrides() -> dict[str, Any]:
    """读用户覆盖文件 data/tts_providers.json（不存在返回空 dict）。

    覆盖文件坏了**不能**让整个语音功能挂掉——按不可信输入处理，读不到就忽略。
    """
    path = data_path("tts_providers.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏文件宁可忽略，也不能让 TTS 整体不可用
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """dict 递归合并（over 优先）；非 dict 值直接覆盖。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _providers() -> dict[str, dict[str, Any]]:
    merged = {k: dict(v) for k, v in BUILTIN.items()}
    for key, over in _user_overrides().items():
        if key in merged and isinstance(over, dict):
            merged[key] = _deep_merge(merged[key], over)
        else:
            merged[key] = over
    return merged


def provider_key(base_url: str) -> str:
    """按 base_url 主机名识别 provider。

    **精确匹配优先于 `*` 兜底**（不按字典顺序）：否则 generic 的 `*` 会抢先命中，
    用户覆盖文件里新增的 provider 就永远轮不到。
    """
    host = _host(base_url)
    if not host:
        return "generic"
    fallback = None
    for key, conf in _providers().items():
        for pat in conf.get("match_hosts") or ():
            pat = str(pat).lower()
            if pat == "*":
                fallback = fallback or key
            elif host == pat or host.endswith("." + pat):
                return key
    return fallback or "generic"


def _model_entry(base_url: str, model: str) -> dict[str, Any]:
    conf = _providers().get(provider_key(base_url)) or {}
    models = conf.get("models") or {}
    entry = models.get(model) or models.get("*") or {}
    return entry if isinstance(entry, dict) else {}


def model_caps(base_url: str, model: str) -> dict[str, Any]:
    """该 provider×model 的能力矩阵（**保证有值**，字段缺失用通用默认）。"""
    caps = _model_entry(base_url, model).get("caps") or {}
    out = {
        "max_chars": int(caps.get("max_chars") or 4000),
        "formats": list(caps.get("formats") or ["mp3"]),
        "passthrough": bool(caps.get("passthrough")),
    }
    return out


def builtin_voices(base_url: str, model: str) -> list[dict[str, Any]]:
    """内置音色清单（官方 `/audio/voices` 返回空时的兜底，可能为空列表）。"""
    voices = _model_entry(base_url, model).get("voices") or []
    out: list[dict[str, Any]] = []
    for v in voices:
        if isinstance(v, dict) and v.get("id"):
            out.append({"id": str(v["id"]), "label": str(v.get("label") or v["id"]),
                        "lang": v.get("lang") or "", "gender": v.get("gender") or ""})
    return out


def default_voice(base_url: str, model: str) -> str:
    """`voice` 留空时使用的默认音色（generic 恒为 alloy，保持旧行为兼容）。"""
    dv = _model_entry(base_url, model).get("default_voice")
    return str(dv) if dv else "alloy"


def effective_config(base_url: str, model: str) -> dict[str, Any]:
    """一次性返回 UI 需要的全部元数据（减少往返）。"""
    key = provider_key(base_url)
    conf = _providers().get(key) or {}
    return {"provider": key, "label": conf.get("label") or key,
            "caps": model_caps(base_url, model),
            "builtin_voices": builtin_voices(base_url, model),
            "default_voice": default_voice(base_url, model)}


# ── 音色发现（需要联网的部分）──────────────────────────
# 三级降级：① 官方接口  ② 内置清单  ③ 批量探测。
# StepFun 实测 /v1/audio/voices 返回 200 但 data 为空 —— 所以①常常拿不到，
# ②③ 才是主力；①仍要先试，因为有些平台（如 OpenAI）是有的。

_CACHE_NAME = "tts_voices_cache.json"


def _voice_candidates(item: Any) -> str:
    """宽容解析官方返回的音色条目（不同平台字段名不一）。"""
    if not isinstance(item, dict):
        return str(item or "").strip()
    for key in ("id", "voice_id", "name", "short_name", "voice"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def fetch_official_voices(base_url: str, api_key: str,
                          timeout: float = 15.0) -> tuple[list[dict[str, Any]], str | None]:
    """官方接口拉音色。

    Returns:
        (音色列表, 失败原因)；接口不存在/失败/为空都返回 `([], None|原因)` ——
        **不抛异常**：官方接口拿不到是常态（StepFun 实测返回空），必须能落到下一级。
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    last_err: str | None = None
    for path in ("/audio/voices", "/voices"):
        url = f"{base_url.rstrip('/')}{path}"
        try:
            with make_client(base_url, timeout=timeout) as client:
                resp = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            last_err = type(exc).__name__
            continue
        if resp.status_code != 200:
            last_err = f"HTTP {resp.status_code}"
            continue
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            last_err = "非 JSON 响应"
            continue
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = payload if isinstance(payload, list) else []
        out: list[dict[str, Any]] = []
        for it in items:
            vid = _voice_candidates(it)
            if not vid:
                continue
            label = ""
            gender = ""
            lang = ""
            if isinstance(it, dict):
                label = str(it.get("label") or it.get("description") or "")
                gender = str(it.get("gender") or "")
                lang = str(it.get("language") or it.get("lang") or "")
            out.append({"id": vid, "label": label or vid, "lang": lang, "gender": gender})
        if out:
            return out, None
        last_err = "接口返回空列表"
    return [], last_err


def load_probe_cache() -> list[dict[str, Any]]:
    """读探测缓存（data/tts_voices_cache.json）。坏文件按空处理。"""
    path = data_path(_CACHE_NAME)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    voices = (data or {}).get("voices") if isinstance(data, dict) else None
    return [v for v in (voices or []) if isinstance(v, dict) and v.get("id")]


def save_probe_cache(voices: list[dict[str, Any]]) -> None:
    """把探测成功的音色写进缓存（供 /tts/voices 的第三级使用）。"""
    path = data_path(_CACHE_NAME)
    payload = {"probed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "voices": voices}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def probe_voices(base_url: str, model: str, api_key: str, voice_ids: list[str],
                 *, timeout: float = 20.0, max_workers: int = 4) -> list[dict[str, Any]]:
    """批量探测音色可用性：对每个 id 发一次最小合成，**只回报告、不返回音频**。

    并发 max_workers 路，控制总时长（探测是低频操作，但也不该让 HTTP 请求挂死）。
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base_url.rstrip('/')}/audio/speech"

    def _one(vid: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        body = {"model": model, "input": "测试", "voice": vid, "response_format": "mp3"}
        try:
            with make_client(base_url, timeout=timeout) as client:
                resp = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return {"id": vid, "ok": False, "http": None, "ms": 0,
                    "error": type(exc).__name__}
        ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code == 200:
            return {"id": vid, "ok": True, "http": 200, "ms": ms, "error": ""}
        hint = ""
        try:
            payload = resp.json()
            err = payload.get("error")
            hint = (err.get("message") if isinstance(err, dict) else "") or str(payload.get("message") or "")
        except Exception:  # noqa: BLE001
            hint = (resp.text or "")[:120]
        return {"id": vid, "ok": False, "http": resp.status_code, "ms": ms, "error": hint[:160]}

    ids = [v for v in dict.fromkeys(voice_ids) if v][:24]
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        results = list(pool.map(_one, ids))
    return results
