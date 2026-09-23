"""设置服务：``settings`` 表读写 + 密钥加密/掩码 + 连接测试（架构文档 §4.3 / §6.10 / §11）。

职责：
- 维护「设置键规范」（默认值 / 是否密文 / 类型）；
- 读写 ``settings`` 表（**唯一**允许直接操作该表的模块）；
- 密文值经 :class:`backend.security.SecurityManager` 加解密，对外只回掩码；
- 提供 ``llm / embed / tts / ollama`` 连接测试（用 httpx 手写，不用 openai SDK）。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..utils.http import make_client

from ..db.connection import get_db
from ..errors import AppError
from ..logging_setup import register_secret
from ..paths import resource_path
from ..security import get_security_manager
from ..utils.timeutil import now_iso

logger = logging.getLogger(__name__)

__all__ = ["SettingSpec", "SettingsService", "SPECS"]


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """单条设置项规范。

    Attributes:
        key: 点分键名（如 ``llm.base_url``）。
        default: 默认值（字符串形态存储）。
        is_secret: 是否密文存储。
        kind: 值类型（``str|int|float|bool``）。
    """

    key: str
    default: str
    is_secret: bool
    kind: str

    @property
    def group(self) -> str:
        """所属分组（键名前缀）。"""
        return self.key.split(".", 1)[0]

    @property
    def field(self) -> str:
        """组内字段名。"""
        return self.key.split(".", 1)[1]


# 设置键规范（顺序即 §6.10 展示顺序）。
SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("llm.base_url", "", False, "str"),
    SettingSpec("llm.model", "", False, "str"),
    SettingSpec("llm.api_key", "", True, "str"),
    SettingSpec("llm.temperature", "0.7", False, "float"),
    SettingSpec("llm.max_tokens", "2048", False, "int"),
    SettingSpec("llm.enable_thinking", "false", False, "bool"),
    SettingSpec("embed.provider", "auto", False, "str"),
    SettingSpec("embed.base_url", "", False, "str"),
    SettingSpec("embed.model", "", False, "str"),
    SettingSpec("embed.api_key", "", True, "str"),
    # 本地嵌入引擎：hash=字符 n-gram 哈希（零下载，默认）；bge=本地 bge-small-zh
    # 语义模型（ONNX INT8 约 24MB，scripts/download_bge_model.py 下载；模型不完整
    # 时自动回退 hash——开关即「语义检索默认关」的用户决策落地）。
    SettingSpec("embed.local_engine", "hash", False, "str"),
    SettingSpec("embed.local_dir", "models/embed/bge-small-zh-v1.5", False, "str"),
    SettingSpec("tts.enabled", "true", False, "bool"),
    SettingSpec("tts.mode", "local", False, "str"),
    SettingSpec("tts.base_url", "", False, "str"),
    SettingSpec("tts.model", "", False, "str"),
    SettingSpec("tts.voice", "alloy", False, "str"),
    SettingSpec("tts.api_key", "", True, "str"),
      # 本地朗读引擎：system=浏览器系统语音（默认，零依赖、即时出声）；
      # melo=本地 MeloTTS 神经语音（更自然但 CPU 合成慢，约 40s/段，且音量不稳）。
      # 2026-09-23 用户实测：MeloTTS 效果不如系统语音，默认改 system（可随时切回）。
      SettingSpec("tts.local_engine", "system", False, "str"),
    # 本地 MeloTTS 模型目录（相对项目根 / exe 同级 / data 均可，见 TTSService.model_dir）。
    SettingSpec("tts.local_dir", "models/tts/melo-zh_en", False, "str"),
    # 自定义 HTTP 语音服务（本地部署的 TTS 项目，自定义协议）：
    # URL 模板 + 请求方法 + body 模板三要素。`{text}` 是唯一占位符 ——
    # URL 里按 URL 编码替换，body 里按 JSON 转义替换。
    SettingSpec("tts.custom_url", "", False, "str"),
    SettingSpec("tts.custom_method", "GET", False, "str"),
    SettingSpec("tts.custom_body", '{"text":"{text}"}', False, "str"),
    SettingSpec("tts.custom_format", "wav", False, "str"),
    SettingSpec("tts.custom_timeout", "60", False, "int"),
    SettingSpec("asr.model_dir", "models/asr/sense-voice-small", False, "str"),
    SettingSpec("asr.language", "zh", False, "str"),
    SettingSpec("ui.guided_default", "false", False, "bool"),
    SettingSpec("ui.locale", "zh-CN", False, "str"),
    SettingSpec("ui.theme", "light", False, "str"),
    SettingSpec("retrieval.top_k", "8", False, "int"),
    SettingSpec("retrieval.hybrid_alpha", "0.5", False, "float"),
    SettingSpec("retrieval.min_vec_score", "0.2", False, "float"),
    SettingSpec("memory.enabled", "true", False, "bool"),
    # 用户画像（首次启动引导收集；每次 AI 生成时作为上下文注入系统提示词）。
    # 存选项 id（英文短 id），注入时由 services/profile.py 转成中文描述。
    SettingSpec("profile.age", "", False, "str"),
    SettingSpec("profile.role", "", False, "str"),
    SettingSpec("profile.stage", "", False, "str"),
    SettingSpec("profile.grade", "", False, "str"),
    SettingSpec("profile.purpose", "", False, "str"),
    SettingSpec("profile.style", "", False, "str"),
    SettingSpec("profile.daily", "", False, "str"),
    SettingSpec("profile.fields", "", False, "str"),      # 常学领域（逗号分隔，多选）
    SettingSpec("profile.note", "", False, "str"),        # 开放题一句话
    # 新手引导：done=完成标志；step=断点（intro/profile/api，刷新后从当前步继续）。
    SettingSpec("guide.done", "false", False, "bool"),
    SettingSpec("guide.step", "", False, "str"),
    # 预生成：讲义/练习完成后，后台提前把下一份资源备好（默认开，用户 2026-09-20 拍板；
    # 关闭后行为与改动前完全一致 —— 不会有任何额外模型调用）。
    SettingSpec("prefetch.enabled", "true", False, "bool"),
)

_SPEC_MAP: dict[str, SettingSpec] = {s.key: s for s in SPECS}

# 连接测试超时（秒）。
_TEST_TIMEOUT = 8.0


def _coerce_bool(value: str) -> bool:
    """把字符串转布尔（``true/1/yes/on`` 为真）。"""
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def upstream_hint(resp: Any) -> str:
    """从上游响应体里抠出可读的失败原因，用于展示给用户。

    只取前 200 字符并压缩空白；**绝不包含任何密钥**（响应体是服务商返回的，
    不含我们的 Key，但仍按不可信文本处理）。解析不出来就退化为纯文本前缀。
    """
    raw = ""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON / 空体都按纯文本处理
        payload = None
    if isinstance(payload, dict):
        for key in ("error.message", "error", "message", "msg", "detail"):
            if key == "error.message":
                em = payload.get("error")
                cand = em.get("message") if isinstance(em, dict) else None
            else:
                cand = payload.get(key)
            if isinstance(cand, str) and cand.strip():
                raw = cand
                break
    if not raw:
        raw = resp.text or ""
    raw = (raw or "")[:200]
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _looks_like_voice_problem(text: str) -> bool:
    """上游报错是否与「音色」有关（用于给出针对性提示）。"""
    t = (text or "").lower()
    return ("voice" in t and ("exist" in t or "invalid" in t or "support" in t)) or "voice_id" in t


class SettingsService:
    """设置读写与连接测试服务（进程级单例）。"""

    def __init__(self) -> None:
        self._sec = get_security_manager()

    # ── 单例 ────────────────────────────────────────────
    _instance: "SettingsService | None" = None

    @classmethod
    def get_instance(cls) -> "SettingsService":
        """返回进程级单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 底层读写 ────────────────────────────────────────
    def _raw_rows(self) -> dict[str, str]:
        """读取 ``settings`` 表全部键值（原样，含密文）。"""
        db = get_db()
        rows = db.query_all("SELECT key, value FROM settings")
        return {r["key"]: (r["value"] or "") for r in rows}

    def get_raw(self, key: str) -> str:
        """读取原始值（密文项返回密文）；未设置时回退默认值。"""
        db = get_db()
        row = db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is not None and row["value"] is not None:
            return str(row["value"])
        spec = _SPEC_MAP.get(key)
        return spec.default if spec else ""

    def get(self, key: str) -> str:
        """读取普通（非密）设置的字符串值。"""
        return self.get_raw(key)

    def get_secret(self, key: str) -> str:
        """读取密文设置并解密为明文（仅供服务端使用，禁止下发前端）。"""
        spec = _SPEC_MAP.get(key)
        raw = self.get_raw(key)
        if not raw:
            return ""
        if spec and spec.is_secret and self._sec.is_encrypted(raw):
            return self._sec.decrypt(raw)
        return raw

    def get_int(self, key: str) -> int:
        """读取整型设置。"""
        try:
            return int(float(self.get_raw(key)))
        except (TypeError, ValueError):
            spec = _SPEC_MAP.get(key)
            return int(float(spec.default)) if spec else 0

    def get_float(self, key: str) -> float:
        """读取浮点设置。"""
        try:
            return float(self.get_raw(key))
        except (TypeError, ValueError):
            spec = _SPEC_MAP.get(key)
            return float(spec.default) if spec else 0.0

    def get_bool(self, key: str) -> bool:
        """读取布尔设置。"""
        return _coerce_bool(self.get_raw(key))

    def set_value(self, key: str, value: str, is_secret: bool) -> None:
        """写入单条设置（密文项自动加密）。

        Args:
            key: 设置键。
            value: 明文值（密文项传入明文，本方法内部加密）。
            is_secret: 是否密文存储。
        """
        db = get_db()
        stored = self._sec.encrypt(value) if is_secret else value
        db.execute(
            "INSERT INTO settings(key, value, is_secret, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "is_secret = excluded.is_secret, updated_at = excluded.updated_at",
            (key, stored, 1 if is_secret else 0, now_iso()),
        )
        if is_secret and value:
            register_secret(value)

    # ── 密钥登记（供日志脱敏）────────────────────────────
    def register_secrets(self) -> None:
        """把所有已存密钥的明文登记进日志脱敏集合。"""
        for spec in SPECS:
            if not spec.is_secret:
                continue
            plain = self.get_secret(spec.key)
            if plain:
                register_secret(plain)

    # ── 公开视图 ────────────────────────────────────────
    def get_public(self) -> dict[str, Any]:
        """构造 ``GET /api/settings`` 的公开视图（**不含明文 Key**）。

        Returns:
            分组后的设置字典，密钥项仅含 ``api_key_set`` 与 ``api_key_masked``。
        """
        result: dict[str, dict[str, Any]] = {}
        for spec in SPECS:
            group = result.setdefault(spec.group, {})
            if spec.is_secret:
                plain = self.get_secret(spec.key)
                group[f"{spec.field}_set"] = bool(plain)
                group[f"{spec.field}_masked"] = self._sec.mask(plain)
                continue
            raw = self.get_raw(spec.key)
            if spec.kind == "int":
                group[spec.field] = self.get_int(spec.key)
            elif spec.kind == "float":
                group[spec.field] = self.get_float(spec.key)
            elif spec.kind == "bool":
                group[spec.field] = _coerce_bool(raw)
            else:
                group[spec.field] = raw

        # ASR 模型可用性（只读派生字段）。
        asr_dir = resource_path("models", "asr", "sense-voice-small")
        result.setdefault("asr", {})["available"] = bool(asr_dir.exists())
        return result

    # ── 更新 ────────────────────────────────────────────
    def update(self, patch: dict[str, Any]) -> list[str]:
        """应用嵌套补丁（如 ``{"llm": {"base_url": "...", "api_key": "sk-..."} }``）。

        Args:
            patch: 分组字典；未出现的键保持不变；密钥项传空串表示「不改动」。

        Returns:
            实际更新到的点分键名列表。
        """
        updated: list[str] = []
        for group, fields in patch.items():
            if not isinstance(fields, dict):
                continue
            for field, value in fields.items():
                key = f"{group}.{field}"
                spec = _SPEC_MAP.get(key)
                if spec is None or value is None:
                    continue
                if spec.is_secret:
                    text = str(value)
                    if text == "":
                        continue  # 空串 = 保持原值不变
                    self.set_value(key, text, True)
                else:
                    self.set_value(key, self._stringify(spec, value), False)
                updated.append(key)
        if updated:
            self.register_secrets()
            logger.info("设置已更新", extra={"extra_fields": {"updated": updated}})
        return updated

    @staticmethod
    def _stringify(spec: SettingSpec, value: Any) -> str:
        """把入参规范化为存储字符串。"""
        if spec.kind == "bool":
            return "true" if _coerce_bool(str(value)) else "false"
        if spec.kind == "int":
            try:
                return str(int(float(value)))
            except (TypeError, ValueError):
                return spec.default
        if spec.kind == "float":
            try:
                return str(float(value))
            except (TypeError, ValueError):
                return spec.default
        return str(value)

    # ── 连接测试 ────────────────────────────────────────
    def test(self, target: str) -> dict[str, Any]:
        """测试指定目标的连通性。

        Args:
            target: ``llm | embed | tts | ollama``。

        Returns:
            ``{"ok": True, "latency_ms": int, "detail": str}``；当端点连通但
            配置仍有隐患（如模型名不在端点可用列表中）时额外带 ``warning``。

        Raises:
            AppError: ``2000`` 配置不完整（缺 base_url / 缺模型名）；``2003`` 连接失败。
        """
        started = time.perf_counter()
        if target == "llm":
            detail, warning = self._test_chat_completions()
        elif target == "embed":
            detail, warning = self._test_embeddings()
        elif target == "tts":
            detail, warning = self._test_tts()
        elif target == "ollama":
            detail, warning = self._test_ollama()
        else:
            raise AppError(1000, "不支持的测试目标", f"target={target}")
        latency = int((time.perf_counter() - started) * 1000)
        result: dict[str, Any] = {"ok": True, "latency_ms": latency, "detail": detail, "message": detail}
        if warning:
            result["warning"] = warning
        return result

    def _http_error(self, target: str, exc: Exception) -> "AppError":
        """把底层 HTTP 异常转成 2003 连接测试失败。"""
        logger.warning("连接测试失败：%s", target, extra={"extra_fields": {"err": type(exc).__name__}})
        return AppError(2003, f"{target} 连接测试失败", f"{type(exc).__name__}")

    def _resolve_embed_base(self) -> str:
        """嵌入端点：``embed.base_url`` 为空时沿用 ``llm.base_url``（与界面提示一致）。"""
        return (self.get("embed.base_url") or self.get("llm.base_url")).rstrip("/")

    @staticmethod
    def _status_problem(resp: Any, target: str) -> "AppError | None":
        """把 HTTP 状态里真正代表失败的类别转成 2003。

        要点：``401/403`` 必须判失败——否则 Key 写错也会显示「连接成功」；
        ``5xx`` 同理。其余 4xx（如 ``404 未提供该接口``）交由各测试方法降级为
        ``warning``，因为「端点没有这个辅助接口」不等于「配置不可用」。
        """
        if resp.status_code in (401, 403):
            return AppError(
                2003,
                f"{target} 鉴权失败（HTTP {resp.status_code}）",
                "API Key 无效、已过期或无权访问该模型",
            )
        if resp.status_code >= 500:
            return AppError(2003, f"{target} 连接测试失败", f"HTTP {resp.status_code}")
        return None

    @staticmethod
    def _model_list_warning(resp: Any, model: str) -> str | None:
        """端点返回模型清单但不含所配置模型名时，给出提示（不判为失败）。

        这是「能连通却问不出话」的最常见原因：接口通、Key 对，但模型名写错或
        写成了别的服务商的模型名。清单解析失败（端点不暴露 / 格式不同）时静默放过。
        """
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001 - 端点不返回 JSON 属正常情况
            return None
        items = payload.get("data") or payload.get("models") or []
        names = set()
        for item in items:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name")
            else:
                name = item
            if name:
                names.add(str(name))
        if not names or model in names:
            return None
        preview = "、".join(sorted(names)[:5])
        more = "…" if len(names) > 5 else ""
        return f"端点连通，但模型名「{model}」不在该端点的可用列表里（可用示例：{preview}{more}）"

    def _test_chat_completions(self) -> tuple[str, str | None]:
        """测试 LLM 端点：GET {base_url}/models（OpenAI 兼容）。"""
        base = self.get("llm.base_url").rstrip("/")
        if not base:
            raise AppError(2000, "尚未填写接口地址（base_url）", "llm.base_url 为空")
        model = self.get("llm.model").strip()
        if not model:
            # 只填地址不填模型名 → 无法提问；这是「提示未配置模型」的头号原因。
            raise AppError(
                2000,
                "尚未填写模型名",
                "llm.model 为空：接口地址与 Key 已保存，但缺少具体模型名（如 deepseek-chat）",
            )
        key = self.get_secret("llm.api_key")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            with make_client(base, timeout=_TEST_TIMEOUT) as client:
                resp = client.get(f"{base}/models", headers=headers)
            problem = self._status_problem(resp, "LLM")
            if problem is not None:
                raise problem
            if resp.status_code == 404:
                return f"HTTP {resp.status_code}", "该端点未提供 /models 列表，已跳过模型名校验；能正常问答即可忽略本条"
            return f"HTTP {resp.status_code}", self._model_list_warning(resp, model)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转 2003
            raise self._http_error("llm", exc) from exc

    def _test_embeddings(self) -> tuple[str, str | None]:
        """测试嵌入端点：POST {base_url}/embeddings。"""
        provider = self.get("embed.provider") or "auto"
        if provider == "local":
            return "本地嵌入（无需联网）", None
        base = self._resolve_embed_base()
        if not base:
            raise AppError(
                2000,
                "嵌入端点未配置",
                "embed.base_url 为空且对话模型地址也为空，无法推断嵌入端点",
            )
        model = self.get("embed.model").strip()
        key = self.get_secret("embed.api_key") or self.get_secret("llm.api_key")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload = {"model": model or "text-embedding-3-small", "input": "ping"}
        warning = (
            None
            if model
            else "未填写嵌入模型名，已用默认名 text-embedding-3-small 试连；若端点不接受该名称会失败"
        )
        try:
            with make_client(base, timeout=_TEST_TIMEOUT) as client:
                resp = client.post(f"{base}/embeddings", headers=headers, json=payload)
            problem = self._status_problem(resp, "嵌入端点")
            if problem is not None:
                raise problem
            if resp.status_code >= 400:
                return f"HTTP {resp.status_code}", (
                    f"该端点未提供嵌入接口（HTTP {resp.status_code}），"
                    "语义检索将退化为本地关键词检索；如需更好的语义匹配请换一个支持 /embeddings 的端点"
                )
            return f"HTTP {resp.status_code}", warning
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._http_error("embed", exc) from exc

    def tts_effective(self) -> tuple[str, str, str]:
        """云端朗读实际使用的 (base_url, model, api_key)。

        端点留空 → 沿用对话模型的端点；Key 留空 → **仅当端点主机与对话模型
        主机相同**时沿用对话模型的 Key。把 A 站点的密钥发往 B 站点属于密钥
        外泄，宁可让请求 401 由用户显式填写。**模型名不回退**——语音模型与
        对话模型完全同名的情况不存在（如 FunAudioLLM/SpeechT5/TTS vs deepseek-chat），
        沿用了必然报错，不如让上层明确提示未配置。
        """
        from urllib.parse import urlsplit

        base = self.get("tts.base_url").strip().rstrip("/")
        model = self.get("tts.model").strip()
        key = self.get_secret("tts.api_key")
        llm_base = self.get("llm.base_url").strip().rstrip("/")
        if not base:
            base = llm_base
        if not key and base and llm_base:
            try:
                same_host = urlsplit(base).netloc.lower() == urlsplit(llm_base).netloc.lower()
            except ValueError:
                same_host = False
            if same_host:
                key = self.get_secret("llm.api_key")
        return base, model, key

    def _test_tts(self) -> tuple[str, str | None]:
        """测试云端 TTS 端点：POST {base_url}/audio/speech。

        所有返回都带上**实际使用的端点**（仅 netloc + path，**绝不打印 Key**），
        让用户能直接看到「请求打到了哪」，而不是只给一个笼统的 HTTP 状态——
        这正是用户「不知道是配置错还是软件 bug」的根因。
        """
        from urllib.parse import urlsplit

        base, model, key = self.tts_effective()
        if not base:
            raise AppError(2000, "TTS 端点未配置", "请填写语音端点 base_url（或先把对话模型的端点配好）")
        # 端点留空、沿用对话模型时给出明确提示，便于用户自查。
        inherited = not self.get("tts.base_url").strip() and bool(self.get("llm.base_url").strip())
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload = {
            "model": model or "tts-1",
            "voice": self.get("tts.voice") or "alloy",
            "input": "测试",
        }
        parsed = urlsplit(base)
        host = parsed.netloc
        req_path = parsed.path.rstrip("/") + "/audio/speech"
        try:
            with make_client(base, timeout=_TEST_TIMEOUT) as client:
                resp = client.post(f"{base}/audio/speech", headers=headers, json=payload)
            problem = self._status_problem(resp, "TTS 端点")
            if problem is not None:
                raise AppError(
                    problem.code, problem.message,
                    f"{problem.detail}（实际请求：{host}{req_path}）",
                )
            if resp.status_code >= 400:
                hint = upstream_hint(resp)
                detail = f"语音端点返回 HTTP {resp.status_code}"
                if hint:
                    detail += f"：{hint}"
                if _looks_like_voice_problem(hint):
                    detail += ("。该服务商可能不支持当前音色——请在「设置 → 语音 → 音色」填写"
                               "该服务商自己的音色名（例如 StepFun 用 cixingnansheng）")
                detail += f"（实际请求：{host}{req_path}）"
                raise AppError(4003, None, detail)
            ok_msg = f"HTTP {resp.status_code}：{host}{req_path} 可用"
            if inherited:
                ok_msg += "（沿用对话模型端点）"
            return ok_msg, None
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._http_error("tts", exc) from exc

    def _test_ollama(self) -> tuple[str, str | None]:
        """测试本机 Ollama：GET {base}/api/tags。"""
        base = self.get("llm.base_url").rstrip("/")
        if not base or "11434" not in base:
            base = "http://127.0.0.1:11434"
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        try:
            with make_client(base, timeout=_TEST_TIMEOUT) as client:
                resp = client.get(f"{base}/api/tags")
            problem = self._status_problem(resp, "Ollama")
            if problem is not None:
                raise problem
            model = self.get("llm.model").strip()
            if not model:
                return f"HTTP {resp.status_code}", (
                    "Ollama 已连通，但还没填模型名；用 ollama list 查看后填入（如 qwen2.5:7b）"
                )
            return f"HTTP {resp.status_code}", self._model_list_warning(resp, model)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._http_error("ollama", exc) from exc

    def list_models(self, target: str = "llm") -> list[str]:
        """从端点 ``GET /models`` 读取可用模型名列表。

        用途：模型名写错是「能连通却问不出话」的头号原因，与其让用户去翻服务商
        文档，不如直接从端点取回来让他点选。

        Args:
            target: ``llm``（对话模型）或 ``embed``（嵌入模型）。

        Returns:
            去重排序后的模型名；端点不返回列表时返回空列表。

        Raises:
            AppError: ``2000`` 未配置端点；``2003`` 连接或鉴权失败。
        """
        if target == "embed":
            base = self._resolve_embed_base()
            key = self.get_secret("embed.api_key") or self.get_secret("llm.api_key")
            label = "嵌入端点"
        elif target == "tts":
            # 云朗读端点：tts.base_url 为空时沿用对话模型端点（tts_effective 已封装该规则）
            base, _model, key = self.tts_effective()
            label = "语音端点"
        else:
            base = self.get("llm.base_url").rstrip("/")
            key = self.get_secret("llm.api_key")
            label = "对话模型端点"
        if not base:
            raise AppError(2000, "尚未填写接口地址（base_url）", f"{target}.base_url 为空")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            with make_client(base, timeout=_TEST_TIMEOUT) as client:
                resp = client.get(f"{base}/models", headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise self._http_error(label, exc) from exc
        problem = self._status_problem(resp, label)
        if problem is not None:
            raise problem
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001 - 端点未提供 /models 属正常情况
            return []
        items = payload.get("data") or payload.get("models") or []
        names: list[str] = []
        for item in items:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name")
            else:
                name = item
            if name:
                names.append(str(name))
        return sorted(set(names))

    def list_ollama_models(self) -> list[str]:
        """列出本机 Ollama 可用模型名（调用 ``/api/tags``）。

        Returns:
            模型名列表；不可用时返回空列表。
        """
        base = "http://127.0.0.1:11434"
        try:
            with make_client(base, timeout=_TEST_TIMEOUT) as client:
                resp = client.get(f"{base}/api/tags")
                resp.raise_for_status()
                payload = resp.json()
            return [m.get("name", "") for m in payload.get("models", []) if m.get("name")]
        except Exception:  # noqa: BLE001 - 不可用时静默返回空
            return []
