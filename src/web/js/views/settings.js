/* 设置页：模型与 API / 语音（ASR 本地 + TTS 三态）。双语：语言包随本文件走。 */
(function () {
  "use strict";

  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const t = (k, v) => window.I18n ? window.I18n.t(k, v) : k;

  window.I18n && window.I18n.merge({
    zh: {
      "settings.llm.title": "对话模型",
      "settings.llm.hint": "OpenAI 兼容端点均可<strong>直接手填</strong>，不必先选预设。<strong>接口地址与模型名两项都必须填</strong>，否则无法提问；填好后点「测试连接」确认可用。",
      "settings.llm.preset": "快速填充",
      "settings.llm.preset.pick": "— 选择服务商 —",
      "settings.llm.preset.custom": "自定义（手动填写）",
      "settings.llm.base": "base_url",
      "settings.llm.model": "模型名",
      "settings.llm.fetch_models": "拉取可用模型",
      "settings.llm.key": "API Key（本地加密存储，不会写入日志或下发到界面）",
      "settings.llm.key.keep": "已配置（{mask}），留空则不修改",
      "settings.llm.key.ph": "sk-...",
      "settings.llm.temp": "温度",
      "settings.llm.maxtok": "最大 tokens",
      "settings.llm.think": "深度思考",
      "settings.llm.think.on": "开启深度思考模式",
      "settings.llm.think.hint": "适用于 Qwen3 等「混合推理」模型：开启后模型回答前会先推理，更细致但也更慢。DeepSeek-R1 等推理模型自身就会思考，无需开启；若你的服务商不认识该参数会报 HTTP 400，关掉即可。",
      "settings.llm.prefetch": "预生成（后台提前备好后面的内容）",
      "settings.llm.prefetch.on": "开启预生成",
      "settings.llm.prefetch.hint": "开启后：讲义生成完顺手把本讲练习备好；打开讲次时提前备好下一讲的讲义 —— 学习时不用等。只前进一步、不会连锁跑完整门课；已有内容一律跳过。关掉后不会有任何额外模型调用。",
      "settings.llm.save": "保存",
      "settings.llm.test": "测试连接",
      "settings.llm.offline": "断网使用：启动本机 Ollama 后，base_url 填 <code>http://127.0.0.1:11434/v1</code>，Key 留空，即可完全离线问答。",
      "settings.pf.title": "关于你（用户画像）",
      "settings.pf.hint": "新手引导里收集的学习画像。每次生成课程 / 讲义 / 练习 / 回答时，会作为背景一起交给模型，用来调整难度、举例与讲法。只保存在本机，随时可改。",
      "settings.pf.replay": "重新看新手引导",
      "settings.pf.saved": "画像已保存",
      "settings.pf.cleared": "画像已清空",
      "settings.em.title": "嵌入模型（用于语义检索）",
      "settings.em.hint": "自动（<code>auto</code>）：配了云端就用云端，连不上自动退回本地；本地（<code>local</code>）：只用本地引擎检索（完全离线）；云端（<code>cloud</code>）：只用云端。<br>不配置也能用 —— 自动退化为本地检索。base_url 留空时<strong>沿用对话模型的地址</strong>，Key 留空时沿用对话模型的 Key。<br>⚠️ 换过嵌入模型后，旧文档需要用新模型重建索引：在「工作台 → 资料库」里对文档点 <strong>↻</strong> 重新解析。",
      "settings.em.provider": "检索方式",
      "settings.em.provider.auto": "自动（auto）— 配了云端用云端，连不上退回本地",
      "settings.em.provider.cloud": "云端（cloud）— 只用云端嵌入",
      "settings.em.provider.local": "本地（local）— 完全离线",
      "settings.em.base": "base_url（可留空，与对话模型相同）",
      "settings.em.model": "模型名",
      "settings.em.local_engine": "本地引擎（默认 hash；bge = 本地语义模型）",
      "settings.em.engine.hash": "hash — 字符哈希（零下载，默认）",
      "settings.em.engine.bge": "bge — 语义模型（约 24MB，默认关闭）",
      "settings.em.local_dir": "本地模型目录（相对随包 models/ 或绝对路径）",
      "settings.em.fetch_models": "拉取可用模型",
      "settings.em.save": "保存嵌入配置",
      "settings.em.key": "嵌入 API Key（留空则沿用对话模型 Key）",
      "settings.em.key.keep": "已配置，留空则不修改",
      "settings.em.bge.ready": "✅ bge 模型已就绪（{dir}）",
      "settings.em.bge.missing": "bge 模型未下载：运行 scripts/download_bge_model.py（约 24MB）；未就绪时自动回退 hash，不影响使用",
      "settings.em.err.no_base": "嵌入端点未配置：请填 base_url，或先配置上面的对话模型地址",
      "settings.em.err.cloud_model": "云端嵌入必须填写模型名（如 BAAI/bge-m3）",
      "settings.em.saved.degraded": "已保存。未填模型名 → 搜索时退化为本地关键词检索",
      "settings.em.saved": "嵌入配置已保存",
      "settings.voice.title": "语音",
      "settings.asr.title": "语音输入（ASR）— 固定本地识别，不调用任何云端服务",
      "settings.asr.pill": "模型{state}",
      "settings.asr.ready": "已就绪",
      "settings.asr.missing": "未下载",
      "settings.asr.loaded": "已加载",
      "settings.asr.lazy": "按需加载",
      "settings.asr.hint": "首次使用会自动下载模型（约 228MB），之后完全离线可用。",
      "settings.asr.download": "下载语音模型",
      "settings.asr.dl_hint": "请在软件目录运行 scripts/download_models.ps1 下载语音模型",
      "settings.tts.title": "朗读（TTS）",
      "settings.tts.mode.off": "关闭",
      "settings.tts.mode.local": "本地朗读（离线，零外发）",
      "settings.tts.mode.cloud": "云端 API",
      "settings.tts.mode.custom": "自定义服务（本地/自建的 TTS 端口）",
      "settings.tts.local_engine": "本地引擎",
      "settings.tts.engine.system": "系统语音（零依赖，音色较机械）",
      "settings.tts.engine.melo": "神经语音 MeloTTS（更自然，中英混读）",
      "settings.tts.hint.melo.ready": "MeloTTS 模型已就绪（中文+英文混读，本地推理、零外发）。",
      "settings.tts.hint.melo.missing": "⚠ 模型未下载：请运行 scripts/download_tts_model.py，或改用「系统语音」。未下载时会自动回退系统语音。",
      "settings.tts.hint.system": "用 Windows 自带音色，无需下载任何模型。",
      "settings.tts.cloud.base": "语音端点 base_url",
      "settings.tts.cloud.base.ph": "留空 = 沿用对话模型端点",
      "settings.tts.cloud.model": "语音模型名",
      "settings.tts.cloud.model.ph": "FunAudioLLM/SpeechT5/TTS",
      "settings.tts.voice": "音色 voice",
      "settings.tts.voice.ph": "留空=服务商默认",
      "settings.tts.fetch_models": "拉取模型",
      "settings.tts.fetch_voices": "拉取音色",
      "settings.tts.probe_voices": "探测可用音色",
      "settings.tts.custom.url": "地址模板（必填，含 <code>{text}</code> 占位符）",
      "settings.tts.custom.url.hint": "文字会按 URL 编码替换进 <code>{text}</code>。与「云端 API」不同，这里可填任意自定义协议 —— 你本地跑的那个 TTS 端口就填这儿。",
      "settings.tts.custom.method": "请求方法",
      "settings.tts.custom.method.get": "GET（参数写网址里）",
      "settings.tts.custom.method.post": "POST（JSON body）",
      "settings.tts.custom.format": "返回格式",
      "settings.tts.custom.timeout": "读超时（秒）",
      "settings.tts.custom.body": "POST body 模板（仅 POST 时用，含 <code>{text}</code>）",
      "settings.tts.custom.body.example": "例：<code>{\"text\":\"{text}\",\"text_lang\":\"zh\",\"ref_audio_path\":\"D:/voice/ref.wav\"}</code>",
      "settings.tts.custom.test": "试听一句（验证连通）",
      "settings.tts.key": "语音 Key（留空则沿用对话模型 Key）",
      "settings.tts.key.keep": "已配置，留空则不修改",
      "settings.tts.key.same_site": "与对话模型同一站点时可留空",
      "settings.tts.test": "测试连接",
      "settings.tts.save": "保存语音设置",
      "settings.tts.preview": "🔊 试听",
      "settings.tts.live.off": "当前生效：已关闭",
      "settings.tts.live.custom": "当前生效：自定义语音服务",
      "settings.tts.live.custom.none": "当前生效：无 —— 自定义服务尚未配置（地址模板为空）",
      "settings.tts.live.cloud": "当前生效：云端朗读",
      "settings.tts.live.cloud.none": "当前生效：无 —— 云端尚未配置（端点或模型名为空）",
      "settings.tts.live.melo": "当前生效：本地神经语音 MeloTTS",
      "settings.tts.live.system": "当前生效：Windows 系统语音（浏览器合成）",
      "settings.tts.voices.empty": "未发现可用音色 —— 点「探测可用音色」试一试。",
      "settings.tts.voices.fill": "点击填入：",
      "settings.tts.voices.filled": "已填入音色 {id}",
      "settings.tts.voices.meta": "音色 {n} 个 · 来源：api {api} / 内置 {builtin} / 探测 {probe} · 默认：{def}",
      "settings.tts.voices.default": "服务商默认",
      "settings.tts.err.save": "保存配置失败：{msg}",
      "settings.tts.fetching.voices": "拉取音色中…",
      "settings.tts.fetching.models": "拉取模型中…",
      "settings.tts.err.voices": "拉取音色失败：{msg}",
      "settings.tts.err.models": "拉取模型失败：{msg}",
      "settings.tts.no_models": "端点未返回模型列表，请手动填写模型名。",
      "settings.tts.models.count": "端点返回 {n} 个模型 —— 点下面的模型名即可填入",
      "settings.tts.models.filled": "已填入模型 {m}",
      "settings.tts.voices.done": "已拉取音色 —— 点下面的音色名即可填入",
      "settings.tts.voices.failed": "拉取失败，详见音色栏提示",
      "settings.tts.probe.running": "探测中…",
      "settings.tts.probe.hint": "探测中…（对每个候选音色发一次短合成，会消耗少量额度）",
      "settings.tts.probe.done": "探测完成：本次可用 {ok} / 共 {n}",
      "settings.tts.probe.already": "（此前已确认 {n} 个）",
      "settings.tts.probe.skipped": "（还有 {n} 个未测，再点一次继续）",
      "settings.tts.probe.bad": "；不可用示例：{id}（{err}）",
      "settings.tts.probe.err": "探测失败：{msg}",
      "settings.tts.test.cloud_only": "仅「云端 API」模式需要测试连接",
      "settings.tts.test.ok": "端点可用",
      "settings.tts.test.err": "❌ {msg}（{url}）",
      "settings.tts.custom.testing": "合成中…（本地模型可能慢，多等几秒）",
      "settings.tts.custom.ok": "✓ 已播放，服务连通",
      "settings.tts.custom.failed": "试听失败：{msg}",
      "settings.tts.custom.err.no_url": "请先填写地址模板（含 {text}）",
      "settings.tts.saved": "语音设置已保存",
      "settings.tts.preview.pick_mode": "请先把朗读模式选为「本地朗读」或「云端 API」",
      "settings.tts.preview.testing": "试听中…",
      "settings.tts.preview.cloud.failed": "❌ 云端合成失败：HTTP {code}{extra}",
      "settings.tts.preview.cloud.failed2": "❌ 云端合成失败（连不上后端或端点）：{msg}",
      "settings.tts.preview.melo.failed": "❌ 本地神经语音合成失败：HTTP {code}",
      "settings.tts.preview.melo.ok": "✅ 本地神经语音 OK：{n} 字节（完全离线）",
      "settings.tts.preview.melo.err": "❌ 本地神经语音失败：{msg}",
      "settings.tts.preview.no_synth": "❌ 当前浏览器不支持本地朗读",
      "settings.tts.preview.play_failed": "❌ 播放失败：{reason}",
      "settings.tts.preview.unknown": "未知原因",
      "settings.tts.preview.system.ok": "共 {n} 个音色，使用：{name}（{lang}）",
      "settings.tts.preview.system.no_zh": "系统默认 ⚠️ 未找到中文音色",
      "settings.tts.preview.system.none": "未检测到音色（首次调用可能为空，请再点一次）",
      "settings.tts.url.effective": "实际请求：{base}/audio/speech",
      "settings.tts.url.none": "实际请求：未配置",
      "settings.common.testing": "测试中…",
      "settings.common.testing_short": "获取中…",
      "settings.err.no_base": "请先填写接口地址 base_url",
      "settings.err.no_model": "请先填写模型名（如 deepseek-flash）",
      "settings.err.save_failed": "保存失败：{msg}",
      "settings.saved": "已保存",
      "settings.saved.guide_done": "已保存，新手引导也完成了",
      "settings.test.ok": "✅ 连接成功（{ms}ms）",
      "settings.test.warn": " · ⚠️ ",
      "settings.models.empty": "端点未返回模型列表，请手动填写模型名。",
      "settings.models.fill": "点击填入：",
      "settings.models.filled": "已填入 {m}",
      "settings.models.err": "获取模型列表失败：{msg}",
    },
    en: {
      "settings.llm.title": "Chat model",
      "settings.llm.hint": "Any OpenAI-compatible endpoint works — just type it in, no preset needed. <strong>Both the API base URL and model name are required</strong>, or questions won't work; click \"Test connection\" to confirm.",
      "settings.llm.preset": "Quick fill",
      "settings.llm.preset.pick": "— Select a provider —",
      "settings.llm.preset.custom": "Custom (fill in manually)",
      "settings.llm.base": "Base URL",
      "settings.llm.model": "Model",
      "settings.llm.fetch_models": "Fetch models",
      "settings.llm.key": "API key (encrypted locally; never logged or shown on screen)",
      "settings.llm.key.keep": "Configured ({mask}); leave empty to keep",
      "settings.llm.key.ph": "sk-...",
      "settings.llm.temp": "Temperature",
      "settings.llm.maxtok": "Max tokens",
      "settings.llm.think": "Deep thinking",
      "settings.llm.think.on": "Enable deep-thinking mode",
      "settings.llm.think.hint": "For \"hybrid reasoning\" models like Qwen3: the model reasons before answering — more thorough but slower. Reasoning models like DeepSeek-R1 think on their own; if your provider doesn't recognize this parameter it returns HTTP 400, so just turn it off.",
      "settings.llm.prefetch": "Prefetch (prepare later content in the background)",
      "settings.llm.prefetch.on": "Enable prefetch",
      "settings.llm.prefetch.hint": "When on: practice is prepared right after a handout is generated, and the next lesson's handout is prepared when you open a lesson — no waiting while studying. It only moves one step forward, never chains through a whole course, and skips anything that already exists. When off, no extra model calls are made.",
      "settings.llm.save": "Save",
      "settings.llm.test": "Test connection",
      "settings.llm.offline": "Offline use: start local Ollama, set the base URL to <code>http://127.0.0.1:11434/v1</code> and leave the key empty for fully offline Q&A.",
      "settings.pf.title": "About you (learner profile)",
      "settings.pf.hint": "The learning profile collected during onboarding. It is sent to the model as background every time a course, handout, practice, or answer is generated, to tune difficulty, examples, and teaching style. Stored locally only; editable anytime.",
      "settings.pf.replay": "Replay onboarding",
      "settings.pf.saved": "Profile saved",
      "settings.pf.cleared": "Profile cleared",
      "settings.em.title": "Embedding model (for semantic search)",
      "settings.em.hint": "Auto (<code>auto</code>): use the cloud when configured, fall back to local if unreachable; Local (<code>local</code>): local engine only (fully offline); Cloud (<code>cloud</code>): cloud only.<br>Works without any configuration — falls back to local search. An empty base URL <strong>reuses the chat model's address</strong>; an empty key reuses the chat model's key.<br>⚠️ After switching embedding models, old documents need re-indexing with the new model: click <strong>↻</strong> on a document in \"Workbench → Library\".",
      "settings.em.provider": "Search mode",
      "settings.em.provider.auto": "Auto — cloud when configured, local fallback",
      "settings.em.provider.cloud": "Cloud — cloud embeddings only",
      "settings.em.provider.local": "Local — fully offline",
      "settings.em.base": "Base URL (optional; same as chat model)",
      "settings.em.model": "Model",
      "settings.em.local_engine": "Local engine (hash by default; bge = local semantic model)",
      "settings.em.engine.hash": "hash — character hashing (zero download, default)",
      "settings.em.engine.bge": "bge — semantic model (~24MB, off by default)",
      "settings.em.local_dir": "Local model directory (relative to bundled models/ or absolute)",
      "settings.em.fetch_models": "Fetch models",
      "settings.em.save": "Save embedding settings",
      "settings.em.key": "Embedding API key (empty = reuse chat model key)",
      "settings.em.key.keep": "Configured; leave empty to keep",
      "settings.em.bge.ready": "✅ bge model ready ({dir})",
      "settings.em.bge.missing": "bge model not downloaded: run scripts/download_bge_model.py (~24MB); falls back to hash automatically until then",
      "settings.em.err.no_base": "Embedding endpoint not configured: fill in a base URL, or configure the chat model above first",
      "settings.em.err.cloud_model": "Cloud embeddings require a model name (e.g. BAAI/bge-m3)",
      "settings.em.saved.degraded": "Saved. No model name → search degrades to local keyword matching",
      "settings.em.saved": "Embedding settings saved",
      "settings.voice.title": "Voice",
      "settings.asr.title": "Speech input (ASR) — always local, no cloud service",
      "settings.asr.pill": "Model {state}",
      "settings.asr.ready": "ready",
      "settings.asr.missing": "not downloaded",
      "settings.asr.loaded": "loaded",
      "settings.asr.lazy": "loads on demand",
      "settings.asr.hint": "The model (~228MB) downloads automatically on first use; fully offline afterwards.",
      "settings.asr.download": "Download voice model",
      "settings.asr.dl_hint": "Run scripts/download_models.ps1 in the app folder to download the voice model",
      "settings.tts.title": "Read aloud (TTS)",
      "settings.tts.mode.off": "Off",
      "settings.tts.mode.local": "Local (offline, zero external traffic)",
      "settings.tts.mode.cloud": "Cloud API",
      "settings.tts.mode.custom": "Custom service (local/self-hosted TTS port)",
      "settings.tts.local_engine": "Local engine",
      "settings.tts.engine.system": "System voice (zero dependency, more robotic)",
      "settings.tts.engine.melo": "Neural voice MeloTTS (more natural, mixed zh/en)",
      "settings.tts.hint.melo.ready": "MeloTTS model ready (mixed Chinese+English, local inference, zero external traffic).",
      "settings.tts.hint.melo.missing": "⚠ Model not downloaded: run scripts/download_tts_model.py, or switch to \"System voice\". Falls back to system voice until downloaded.",
      "settings.tts.hint.system": "Uses Windows built-in voices; no model download needed.",
      "settings.tts.cloud.base": "Voice endpoint base URL",
      "settings.tts.cloud.base.ph": "Empty = reuse chat model endpoint",
      "settings.tts.cloud.model": "Voice model",
      "settings.tts.cloud.model.ph": "FunAudioLLM/SpeechT5/TTS",
      "settings.tts.voice": "Voice",
      "settings.tts.voice.ph": "Empty = provider default",
      "settings.tts.fetch_models": "Fetch models",
      "settings.tts.fetch_voices": "Fetch voices",
      "settings.tts.probe_voices": "Probe available voices",
      "settings.tts.custom.url": "URL template (required, with <code>{text}</code> placeholder)",
      "settings.tts.custom.url.hint": "Text is URL-encoded into <code>{text}</code>. Unlike \"Cloud API\", any custom protocol works here — point it at the TTS port running on your machine.",
      "settings.tts.custom.method": "Method",
      "settings.tts.custom.method.get": "GET (params in the URL)",
      "settings.tts.custom.method.post": "POST (JSON body)",
      "settings.tts.custom.format": "Format",
      "settings.tts.custom.timeout": "Read timeout (s)",
      "settings.tts.custom.body": "POST body template (POST only, with <code>{text}</code>)",
      "settings.tts.custom.body.example": "Example: <code>{\"text\":\"{text}\",\"text_lang\":\"zh\",\"ref_audio_path\":\"D:/voice/ref.wav\"}</code>",
      "settings.tts.custom.test": "Preview one line (check connectivity)",
      "settings.tts.key": "Voice key (empty = reuse chat model key)",
      "settings.tts.key.keep": "Configured; leave empty to keep",
      "settings.tts.key.same_site": "May be empty when on the same site as the chat model",
      "settings.tts.test": "Test connection",
      "settings.tts.save": "Save voice settings",
      "settings.tts.preview": "🔊 Preview",
      "settings.tts.live.off": "Active: off",
      "settings.tts.live.custom": "Active: custom voice service",
      "settings.tts.live.custom.none": "Active: none — custom service not configured (URL template empty)",
      "settings.tts.live.cloud": "Active: cloud read-aloud",
      "settings.tts.live.cloud.none": "Active: none — cloud not configured (endpoint or model empty)",
      "settings.tts.live.melo": "Active: local neural voice MeloTTS",
      "settings.tts.live.system": "Active: Windows system voice (browser synthesis)",
      "settings.tts.voices.empty": "No voices found — try \"Probe available voices\".",
      "settings.tts.voices.fill": "Click to fill:",
      "settings.tts.voices.filled": "Filled voice {id}",
      "settings.tts.voices.meta": "{n} voices · sources: api {api} / built-in {builtin} / probe {probe} · default: {def}",
      "settings.tts.voices.default": "provider default",
      "settings.tts.err.save": "Failed to save settings: {msg}",
      "settings.tts.fetching.voices": "Fetching voices…",
      "settings.tts.fetching.models": "Fetching models…",
      "settings.tts.err.voices": "Failed to fetch voices: {msg}",
      "settings.tts.err.models": "Failed to fetch models: {msg}",
      "settings.tts.no_models": "The endpoint returned no model list; enter the model name manually.",
      "settings.tts.models.count": "Endpoint returned {n} models — click a name below to fill it in",
      "settings.tts.models.filled": "Filled model {m}",
      "settings.tts.voices.done": "Voices fetched — click a name below to fill it in",
      "settings.tts.voices.failed": "Fetch failed; see the voice section for details",
      "settings.tts.probe.running": "Probing…",
      "settings.tts.probe.hint": "Probing… (one short synthesis per candidate voice; uses a little quota)",
      "settings.tts.probe.done": "Probe done: {ok} available this round / {n} total",
      "settings.tts.probe.already": "({n} confirmed earlier)",
      "settings.tts.probe.skipped": "({n} untested; click again to continue)",
      "settings.tts.probe.bad": "; unavailable example: {id} ({err})",
      "settings.tts.probe.err": "Probe failed: {msg}",
      "settings.tts.test.cloud_only": "Test connection is only needed in \"Cloud API\" mode",
      "settings.tts.test.ok": "Endpoint available",
      "settings.tts.test.err": "❌ {msg} ({url})",
      "settings.tts.custom.testing": "Synthesizing… (local model may be slow; wait a few seconds)",
      "settings.tts.custom.ok": "✓ Played — service reachable",
      "settings.tts.custom.failed": "Preview failed: {msg}",
      "settings.tts.custom.err.no_url": "Fill in the URL template first (with {text})",
      "settings.tts.saved": "Voice settings saved",
      "settings.tts.preview.pick_mode": "Pick \"Local\" or \"Cloud API\" read-aloud mode first",
      "settings.tts.preview.testing": "Previewing…",
      "settings.tts.preview.cloud.failed": "❌ Cloud synthesis failed: HTTP {code}{extra}",
      "settings.tts.preview.cloud.failed2": "❌ Cloud synthesis failed (backend or endpoint unreachable): {msg}",
      "settings.tts.preview.melo.failed": "❌ Local neural synthesis failed: HTTP {code}",
      "settings.tts.preview.melo.ok": "✅ Local neural voice OK: {n} bytes (fully offline)",
      "settings.tts.preview.melo.err": "❌ Local neural voice failed: {msg}",
      "settings.tts.preview.no_synth": "❌ This browser does not support local read-aloud",
      "settings.tts.preview.play_failed": "❌ Playback failed: {reason}",
      "settings.tts.preview.unknown": "unknown reason",
      "settings.tts.preview.system.ok": "{n} voices; using: {name} ({lang})",
      "settings.tts.preview.system.no_zh": "system default ⚠️ no Chinese voice found",
      "settings.tts.preview.system.none": "No voices detected (may be empty on first call; try again)",
      "settings.tts.url.effective": "Actual request: {base}/audio/speech",
      "settings.tts.url.none": "Actual request: not configured",
      "settings.common.testing": "Testing…",
      "settings.common.testing_short": "Loading…",
      "settings.err.no_base": "Fill in the API base URL first",
      "settings.err.no_model": "Fill in the model name first (e.g. deepseek-flash)",
      "settings.err.save_failed": "Save failed: {msg}",
      "settings.saved": "Saved",
      "settings.saved.guide_done": "Saved — onboarding completed too",
      "settings.test.ok": "✅ Connected ({ms}ms)",
      "settings.test.warn": " · ⚠️ ",
      "settings.models.empty": "The endpoint returned no model list; enter the model name manually.",
      "settings.models.fill": "Click to fill:",
      "settings.models.filled": "Filled {m}",
      "settings.models.err": "Failed to fetch model list: {msg}",
    },
  });

  const PRESETS = [
    // 模型名以官方当前值为准：deepseek-chat / deepseek-reasoner 已退役，
    // 现在直接用 deepseek-flash（Flash 无需再指定 4.0/4.1 这类版本号）。
    ["https://api.deepseek.com", "deepseek-flash", "DeepSeek"],
    ["https://open.bigmodel.cn/api/paas/v4", "glm-4-plus", "智谱 GLM"],
    ["https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "阿里百炼"],
    ["https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct", "SiliconFlow"],
    ["http://127.0.0.1:11434/v1", "", "Ollama（本地）"],
  ];
  const PRESETS_EN = ["DeepSeek", "Zhipu GLM", "Aliyun Bailian", "SiliconFlow", "Ollama (local)"];

  // 本地 MeloTTS 模型是否就位（来自 /api/tts/status，设置页提示与降级判断用）。
  let TtsModelReady = false;

  // 最近一次 GET /api/settings 里的 guide 分组（保存对话模型后据此判断要不要自动完成引导）。
  let _guide = {};

  async function render(host) {
    const cfg = await Api.get("/api/settings");
    _guide = cfg.guide || {};
    const asr = await Api.get("/api/asr/status");
    let tts = await Api.get("/api/tts/status");
    TtsModelReady = !!(tts && tts.local_model_available);
    const en = (window.I18n && I18n.get() === "en");
    const pName = (i) => (en ? PRESETS_EN[i] : PRESETS[i][2]);

    host.innerHTML = "";
    const page = document.createElement("div");
    page.className = "page";
    page.innerHTML = `
      <div class="card" id="llm-card">
        <h3 style="margin:0 0 4px">${t("settings.llm.title")}</h3>
        <p class="hint" style="margin-top:0">${t("settings.llm.hint")}</p>
        <div class="field"><label>${t("settings.llm.preset")}</label>
          <select id="st-preset"><option value="">${t("settings.llm.preset.pick")}</option>
            ${PRESETS.map((p, i) => `<option value="${i}">${esc(pName(i))}</option>`).join("")}
            <option value="custom">${t("settings.llm.preset.custom")}</option>
          </select></div>
        <div class="row">
          <div class="field"><label>${t("settings.llm.base")}</label><input type="text" id="st-base" value="${esc(cfg.llm.base_url)}" placeholder="https://api.deepseek.com"></div>
          <div class="field" style="max-width:280px"><label>${t("settings.llm.model")}</label>
            <input type="text" id="st-model" value="${esc(cfg.llm.model)}" placeholder="deepseek-flash">
            <button class="btn small" id="st-models" style="margin-top:6px">${t("settings.llm.fetch_models")}</button>
            <div id="st-model-list" class="hint" style="margin-top:6px"></div>
          </div>
        </div>
        <div class="row">
          <div class="field"><label>${t("settings.llm.key")}</label>
            <input type="password" id="st-key" placeholder="${cfg.llm.api_key_set ? t("settings.llm.key.keep", { mask: esc(cfg.llm.api_key_masked) }) : t("settings.llm.key.ph")}"></div>
          <div class="field" style="max-width:120px"><label>${t("settings.llm.temp")}</label><input type="text" id="st-temp" value="${esc(cfg.llm.temperature)}"></div>
          <div class="field" style="max-width:140px"><label>${t("settings.llm.maxtok")}</label><input type="text" id="st-maxtok" value="${esc(cfg.llm.max_tokens)}"></div>
        </div>
        <div class="field">
          <label>${t("settings.llm.think")}</label>
          <label class="switch"><input type="checkbox" id="st-think"${cfg.llm.enable_thinking ? " checked" : ""}> ${t("settings.llm.think.on")}</label>
          <div class="hint" style="margin-top:6px">${t("settings.llm.think.hint")}</div>
        </div>
        <div class="field">
          <label>${t("settings.llm.prefetch")}</label>
          <label class="switch"><input type="checkbox" id="st-prefetch"${(cfg.prefetch && cfg.prefetch.enabled) ? " checked" : ""}> ${t("settings.llm.prefetch.on")}</label>
          <div class="hint" style="margin-top:6px">${t("settings.llm.prefetch.hint")}</div>
        </div>
        <div class="row">
          <button class="btn primary" id="st-save">${t("settings.llm.save")}</button>
          <button class="btn" id="st-test">${t("settings.llm.test")}</button>
          <span class="hint" id="st-test-result"></span>
        </div>
        <p class="hint">${t("settings.llm.offline")}</p>
      </div>

      <div class="card" id="pf-card">
        <h3 style="margin:0 0 4px">${t("settings.pf.title")}</h3>
        <p class="hint" style="margin-top:0">
          ${t("settings.pf.hint")}
        </p>
        <div id="pf-editor"></div>
        <div class="row" style="margin-top:10px">
          <button class="btn small" id="pf-replay">${t("settings.pf.replay")}</button>
          <span class="hint" id="pf-result"></span>
        </div>
      </div>

      <div class="card">
        <h3 style="margin:0 0 4px">${t("settings.em.title")}</h3>
        <p class="hint" style="margin-top:0">
          ${t("settings.em.hint")}
        </p>
        <div class="row">
          <div class="field" style="max-width:260px"><label>${t("settings.em.provider")}</label>
            <select id="em-provider">
              ${[["auto", t("settings.em.provider.auto")],
                 ["cloud", t("settings.em.provider.cloud")],
                 ["local", t("settings.em.provider.local")]]
                .map(([v, n]) => `<option value="${v}" ${cfg.embed.provider === v ? "selected" : ""}>${n}</option>`).join("")}
            </select></div>
          <div class="field"><label>${t("settings.em.base")}</label><input type="text" id="em-base" value="${esc(cfg.embed.base_url)}"></div>
          <div class="field"><label>${t("settings.em.model")}</label><input type="text" id="em-model" value="${esc(cfg.embed.model)}" placeholder="text-embedding-3-small"></div>
        </div>
        <div class="row">
          <div class="field" style="max-width:220px"><label>${t("settings.em.local_engine")}</label>
            <select id="em-local-engine">
              <option value="hash" ${cfg.embed.local_engine !== "bge" ? "selected" : ""}>${t("settings.em.engine.hash")}</option>
              <option value="bge" ${cfg.embed.local_engine === "bge" ? "selected" : ""}>${t("settings.em.engine.bge")}</option>
            </select>
            <div class="hint" id="em-bge-status" style="margin-top:4px"></div>
          </div>
          <div class="field"><label>${t("settings.em.local_dir")}</label><input type="text" id="em-local-dir" value="${esc(cfg.embed.local_dir || "models/embed/bge-small-zh-v1.5")}"></div>
        </div>
        <div class="row">
          <button class="btn small" id="em-models">${t("settings.em.fetch_models")}</button>
          <div id="em-model-list" class="hint"></div>
        </div>
        <div class="row">
          <div class="field"><label>${t("settings.em.key")}</label><input type="password" id="em-key" placeholder="${esc(cfg.embed.api_key_set ? t("settings.em.key.keep") : "")}"></div>
          <button class="btn primary" id="em-save">${t("settings.em.save")}</button>
        </div>
      </div>

      <div class="card">
        <h3 style="margin:0 0 4px">${t("settings.voice.title")}</h3>
        <div class="field">
          <label>${t("settings.asr.title")}</label>
          <div class="row">
            <span class="pill ${asr.available ? "ok" : "bad"}">${t("settings.asr.pill", { state: asr.available ? t("settings.asr.ready") : t("settings.asr.missing") })}</span>
            <span class="pill">${asr.loaded ? t("settings.asr.loaded") : t("settings.asr.lazy")}</span>
            <span class="hint">${t("settings.asr.hint")}</span>
            ${asr.available ? "" : `<button class="btn small" id="asr-dl">${t("settings.asr.download")}</button>`}
          </div>
        </div>
        <div class="field">
          <label>${t("settings.tts.title")}</label>
          <div class="row">
            <select id="tts-mode" class="tts-mode-sel" style="padding:8px 10px;border:1px solid var(--border);border-radius:8px">
              ${[["off", t("settings.tts.mode.off")], ["local", t("settings.tts.mode.local")], ["cloud", t("settings.tts.mode.cloud")],
                 ["custom", t("settings.tts.mode.custom")]]
                .map(([v, n]) => `<option value="${v}" ${tts.mode === v ? "selected" : ""}>${n}</option>`).join("")}
            </select>
            <div class="field" id="tts-local-wrap">
              <label>${t("settings.tts.local_engine")}</label>
              <select id="tts-engine" style="padding:8px 10px;border:1px solid var(--border);border-radius:8px">
                ${[["system", t("settings.tts.engine.system")], ["melo", t("settings.tts.engine.melo")]]
                  .map(([v, n]) => `<option value="${v}" ${(cfg.tts.local_engine || "melo") === v ? "selected" : ""}>${n}</option>`).join("")}
              </select>
              <span class="hint" id="tts-engine-hint"></span>
            </div>
          </div>
          <div class="row" id="tts-cloud-row">
            <div class="field" id="tts-cloud-wrap"><label>${t("settings.tts.cloud.base")}</label><input type="text" id="tts-base" value="${esc(cfg.tts.base_url)}" placeholder="${t("settings.tts.cloud.base.ph")}"></div>
            <div class="field" id="tts-cloud-model"><label>${t("settings.tts.cloud.model")}</label><input type="text" id="tts-model" value="${esc(cfg.tts.model)}" placeholder="FunAudioLLM/SpeechT5/TTS"></div>
            <div class="field" id="tts-voice-wrap"><label>${t("settings.tts.voice")}</label><input type="text" id="tts-voice" value="${esc(cfg.tts.voice || "")}" placeholder="${t("settings.tts.voice.ph")}"><span class="hint" id="tts-voice-meta"></span></div>
          </div>
          <div class="row" id="tts-discover-row">
            <button class="btn small" id="tts-models">${t("settings.tts.fetch_models")}</button>
            <button class="btn small" id="tts-voices-fetch">${t("settings.tts.fetch_voices")}</button>
            <button class="btn small" id="tts-voices-probe">${t("settings.tts.probe_voices")}</button>
          </div>
          <p class="hint" id="tts-discover-result"></p>
          <!-- 列表用「点击填入」按钮（与对话模型选择器同一模式）。
               不能用 <datalist>：浏览器会按输入框已有值**过滤**选项 ——
               模型名里已有 stepaudio-2.5-tts 时 9 个模型只剩 1 个可选，
               音色里是 livelybreezy-female 时 8 个内置音色一个都不显示。 -->
          <div id="tts-model-list" class="hint"></div>
          <div id="tts-voice-list" class="hint"></div>
          <div id="tts-custom-wrap" style="display:none">
            <div class="field">
              <label>${t("settings.tts.custom.url")}</label>
              <input type="text" id="tts-custom-url" value="${esc(cfg.tts.custom_url || "")}"
                     placeholder="http://127.0.0.1:9880/tts?text={text}&text_lang=zh&media_type=wav">
              <div class="hint">${t("settings.tts.custom.url.hint")}</div>
            </div>
            <div class="row">
              <div class="field" style="max-width:150px">
                <label>${t("settings.tts.custom.method")}</label>
                <select id="tts-custom-method">
                  ${[["GET", t("settings.tts.custom.method.get")], ["POST", t("settings.tts.custom.method.post")]]
                    .map(([v, n]) => `<option value="${v}" ${(cfg.tts.custom_method || "GET") === v ? "selected" : ""}>${n}</option>`).join("")}
                </select>
              </div>
              <div class="field" style="max-width:150px">
                <label>${t("settings.tts.custom.format")}</label>
                <select id="tts-custom-format">
                  ${[["wav", "wav"], ["mp3", "mp3"], ["ogg", "ogg"]]
                    .map(([v, n]) => `<option value="${v}" ${(cfg.tts.custom_format || "wav") === v ? "selected" : ""}>${n}</option>`).join("")}
                </select>
              </div>
              <div class="field" style="max-width:130px">
                <label>${t("settings.tts.custom.timeout")}</label>
                <input type="text" id="tts-custom-timeout" value="${esc(String(cfg.tts.custom_timeout || 60))}">
              </div>
            </div>
            <div class="field">
              <label>${t("settings.tts.custom.body")}</label>
              <input type="text" id="tts-custom-body" value="${esc(cfg.tts.custom_body || '{"text":"{text}"}')}">
              <div class="hint">${t("settings.tts.custom.body.example")}</div>
            </div>
            <div class="row">
              <button class="btn small" id="tts-custom-test">${t("settings.tts.custom.test")}</button>
              <span class="hint" id="tts-custom-test-result"></span>
            </div>
          </div>
          <div class="row" id="tts-key-row">
            <div class="field"><label>${t("settings.tts.key")}</label><input type="password" id="tts-key" placeholder="${esc(cfg.tts.api_key_set ? t("settings.tts.key.keep") : t("settings.tts.key.same_site"))}"></div>
          </div>
          <p class="hint" id="tts-live"></p>
          <div class="row">
            <button class="btn" id="tts-test">${t("settings.tts.test")}</button>
            <button class="btn primary" id="tts-save">${t("settings.tts.save")}</button>
            <button class="btn" id="tts-preview">${t("settings.tts.preview")}</button>
          </div>
          <!-- 结果单独成行：文案很长（含 HTTP 状态与实际请求 URL），
               塞在按钮同一行会把按钮挤到换行，导致「保存语音设/置」这种断词。 -->
          <p class="hint" id="tts-test-result"></p>
          <p class="hint" id="tts-preview-result"></p>
          <p class="hint">${en
            ? "The voice endpoint can <strong>differ</strong> from the chat model: on the <strong>same site</strong> both the endpoint and key <strong>can be left empty</strong> — the app reuses the chat model's address and key automatically (<strong>no second key needed</strong>); on a <strong>different voice site</strong>, fill in that site's address and its own key (the chat model's key is never misused). Local read-aloud calls Windows system voices (e.g. Microsoft Huihui) from the browser — fully offline, zero keys. The switch is at the bottom of the workbench; when on, every new reply is read aloud."
            : "语音端点可与对话模型<strong>不同</strong>：<strong>同一个站点</strong>时端点与 Key <strong>都可以留空</strong>，程序自动沿用对话模型的地址与 Key（<strong>不需要另外申请第二个 Key</strong>）；<strong>换成别的语音站点</strong>时，才需要填该站点的地址与那家站点自己的 Key（此时不会误用对话模型的 Key）。本地朗读由浏览器调用 Windows 系统语音（如 Microsoft Huihui），完全离线、零 Key。开关在工作台底部；开启后每条新回答都会朗读。"}</p>
        </div>
      </div>`;
    host.appendChild(page);

    // ── 用户画像（题目与渲染在 js/profile_fields.js）──────────
    // 编辑器是「所见即所存」的全量保存（含跳过的空项），与向导的增量保存不同：
    // 用户在设置页看到的状态就是真相。清空 = 所有字段传空串。
    window.Profile.renderEditor(document.getElementById("pf-editor"), {
      answers: window.Profile.fromSettings(cfg),
      onSave: (a) => {
        Api.put("/api/settings", { profile: window.Profile.toPayload(a) })
          .then(() => { Toast(t("settings.pf.saved")); })
          .catch((e) => Toast(e.message, true));
      },
      onClear: () => {
        Api.put("/api/settings", { profile: window.Profile.toPayload({}) })
          .then(() => { Toast(t("settings.pf.cleared")); })
          .catch((e) => Toast(e.message, true));
      },
    });
    document.getElementById("pf-replay").onclick = () => {
      Api.put("/api/settings", { guide: { done: false, step: "intro" } })
        .then(() => window.Main.refreshModelBadge())
        .then(() => { location.hash = "#/welcome"; })
        .catch((e) => Toast(e.message, true));
    };
    // 新手引导第 3 步「去设置页配置」跳过来：定位并闪烁高亮「对话模型」卡。
    if (/[?&]focus=api\b/.test(location.hash)) {
      const card = document.getElementById("llm-card");
      if (card) {
        card.scrollIntoView({ behavior: "smooth", block: "start" });
        card.classList.add("focus-flash");
        setTimeout(() => card.classList.remove("focus-flash"), 2000);
      }
    }

    // 预设
    document.getElementById("st-preset").onchange = (e) => {
      if (e.target.value === "custom") {
        // 自定义：清空三项并聚焦地址，方便直接粘贴自己的服务商地址。
        ["st-base", "st-model", "st-key"].forEach((id) => {
          document.getElementById(id).value = "";
        });
        const baseEl = document.getElementById("st-base");
        baseEl.placeholder = en ? "https://your-provider/v1" : "https://你的服务商地址/v1";
        baseEl.focus();
        return;
      }
      const p = PRESETS[e.target.value];
      if (!p) return;
      document.getElementById("st-base").value = p[0];
      document.getElementById("st-model").value = p[1] || "";
      if (p[2].indexOf("Ollama") === 0) {
        document.getElementById("st-key").value = "";
        Toast(en
          ? "Local Ollama still needs a model name: run `ollama list` and fill one in, e.g. qwen2.5:7b"
          : "本地 Ollama 还需填模型名：用 ollama list 查看后填入，如 qwen2.5:7b", true);
      }
    };
    document.getElementById("st-save").onclick = async () => {
      const base = val("st-base");
      const model = val("st-model");
      // 只填地址不填模型名 = 无法提问；必须在保存时就说清楚，不能静默通过。
      if (!base) { document.getElementById("st-base").focus(); return Toast(t("settings.err.no_base"), true); }
      if (!model) { document.getElementById("st-model").focus(); return Toast(t("settings.err.no_model"), true); }
      const patch = {
        llm: {
          base_url: base, model: model,
          temperature: parseFloat(val("st-temp")) || 0.7,
          max_tokens: parseInt(val("st-maxtok"), 10) || 2048,
          enable_thinking: !!(document.getElementById("st-think") || {}).checked,
        },
        // 预生成是独立分组（后端 SPECS 的 prefetch.enabled），别塞进 llm。
        prefetch: { enabled: !!(document.getElementById("st-prefetch") || {}).checked },
      };
      const key = val("st-key");
      if (key) patch.llm.api_key = key;
      try {
        await Api.put("/api/settings", patch);
      } catch (e) {
        return Toast(t("settings.err.save_failed", { msg: e.detail || e.message }), true);
      }
      let msg = t("settings.saved");
      // 引导正卡在 api 步且未完成 → 用户在此页配好了 API，保存即视为完成引导。
      if (_guide && _guide.step === "api" && !_guide.done) {
        try {
          await Api.put("/api/settings", { guide: { step: "", done: true } });
          _guide = { step: "", done: true };
          msg = t("settings.saved.guide_done");
        } catch (e) { /* 引导收尾失败不覆盖「已保存」提示 */ }
      }
      Toast(msg); Main.refreshModelBadge();
    };
    document.getElementById("st-test").onclick = async () => {
      const out = document.getElementById("st-test-result");
      const base = val("st-base"), model = val("st-model");
      if (!base) { document.getElementById("st-base").focus(); return Toast(t("settings.err.no_base"), true); }
      if (!model) { document.getElementById("st-model").focus(); return Toast(t("settings.err.no_model"), true); }
      out.textContent = t("settings.common.testing");
      await saveIfChanged();
      try {
        const r = await Api.post("/api/settings/test", { target: "llm" });
        out.textContent = t("settings.test.ok", { ms: r.latency_ms }) + (r.warning ? t("settings.test.warn") + r.warning : "");
        if (r.warning) Toast(r.warning, true);
        Main.refreshModelBadge();
      } catch (e) { out.textContent = "❌ " + e.message; }
    };

    document.getElementById("em-save").onclick = async () => {
      const provider = val("em-provider");
      const base = val("em-base"), model = val("em-model");
      if (provider !== "local" && !base && !cfg.llm.base_url) {
        return Toast(t("settings.em.err.no_base"), true);
      }
      if (provider === "cloud" && !model) {
        document.getElementById("em-model").focus();
        return Toast(t("settings.em.err.cloud_model"), true);
      }
      const patch = {
        embed: {
          provider: provider, base_url: base, model: model,
          local_engine: val("em-local-engine") || "hash",
          local_dir: val("em-local-dir") || "",
        },
      };
      const key = val("em-key");
      if (key) patch.embed.api_key = key;
      await Api.put("/api/settings", patch);
      if (provider !== "local" && !model) {
        Toast(t("settings.em.saved.degraded"), true);
      } else {
        Toast(t("settings.em.saved"));
      }
    };

    // bge 模型就绪状态（只读展示；下载用 scripts/download_bge_model.py）
    Api.get("/api/settings/embed/status").then((st) => {
      const el2 = document.getElementById("em-bge-status");
      if (el2 && st) {
        el2.textContent = st.bge_available
          ? t("settings.em.bge.ready", { dir: st.bge_dir || "" })
          : t("settings.em.bge.missing");
      }
    }).catch(() => { /* 状态拿不到就不显示 */ });

    document.getElementById("tts-mode").onchange = (e) => updateTTSVis(e.target.value);
    updateTTSVis(cfg.tts.mode);

    // 自定义服务的「试听一句」：先把当前填的参数存下来（否则后端读到的还是旧配置），
    // 再直接打 /api/tts/speech —— 不用另开后端接口，还能顺带验证地址模板真的对。
    const cusTest = document.getElementById("tts-custom-test");
    if (cusTest) cusTest.onclick = async () => {
      const out = document.getElementById("tts-custom-test-result");
      const url = val("tts-custom-url");
      if (!url) return Toast(t("settings.tts.custom.err.no_url"), true);
      cusTest.disabled = true;
      if (out) out.textContent = t("settings.tts.custom.testing");
      try {
        await Api.put("/api/settings", { tts: {
          enabled: true, mode: "custom", custom_url: url,
          custom_method: ((document.getElementById("tts-custom-method") || {}).value) || "GET",
          custom_body: val("tts-custom-body"),
          custom_format: ((document.getElementById("tts-custom-format") || {}).value) || "wav",
          custom_timeout: parseInt(val("tts-custom-timeout"), 10) || 60,
        }});
        const resp = await fetch("/api/tts/speech", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: "你好，这是语音测试。" }),
        });
        if (!resp.ok) {
          let msg = "HTTP " + resp.status;
          try {
            const j = await resp.json();
            msg = [j.message, (j.error && j.error.detail) || j.detail].filter(Boolean).join(" —— ") || msg;
          } catch (e) { /* 非 JSON 就用状态码 */ }
          throw new Error(msg);
        }
        const blob = await resp.blob();
        await new Audio(URL.createObjectURL(blob)).play();
        if (out) out.textContent = t("settings.tts.custom.ok");
      } catch (e) {
        if (out) out.textContent = "";
        Toast(t("settings.tts.custom.failed", { msg: e.message }), true);
      } finally { cusTest.disabled = false; }
    };
    const engSel = document.getElementById("tts-engine");
    if (engSel) engSel.onchange = updateEngineHint;

  /** 把语音表单当前值提交到后端（测试/试听/自动发现前必须先提交，否则测的是旧配置）。 */
  async function pushTtsForm() {
      const mode = document.getElementById("tts-mode").value;
      const eng = document.getElementById("tts-engine");
      const patch = {
        tts: {
          enabled: mode !== "off", mode,
          base_url: val("tts-base"), model: val("tts-model"),
          voice: val("tts-voice"),
          local_engine: eng ? eng.value : "system",
          // 自定义服务（自定义协议）：地址模板 + 方法 + body 模板。
          custom_url: val("tts-custom-url"),
          custom_method: ((document.getElementById("tts-custom-method") || {}).value) || "GET",
          custom_body: val("tts-custom-body"),
          custom_format: ((document.getElementById("tts-custom-format") || {}).value) || "wav",
          custom_timeout: parseInt(val("tts-custom-timeout"), 10) || 60,
        },
      };
      const key = val("tts-key");
      if (key) patch.tts.api_key = key;
      await Api.put("/api/settings", patch);
    }

    /** 刷新「当前生效引擎」提示（保存到后端、或切模式后调用）。 */
    function refreshTtsLive() {
      const live = document.getElementById("tts-live");
      if (!live) return;
      let key = "settings.tts.live.off";
      if (tts.enabled && tts.mode === "custom") {
        key = tts.custom_configured ? "settings.tts.live.custom" : "settings.tts.live.custom.none";
      } else if (tts.enabled && tts.mode === "cloud") {
        key = tts.cloud_configured ? "settings.tts.live.cloud" : "settings.tts.live.cloud.none";
      } else if (tts.enabled && tts.mode === "local") {
        key = ((tts.local_engine || "system") === "melo" && tts.local_model_available)
          ? "settings.tts.live.melo" : "settings.tts.live.system";
      }
      live.textContent = t(key);
    }
    refreshTtsLive();

    // ── 云端自动发现（docs/07 P1）：模型 / 音色三级降级 ────────────────
    // 音色不靠「手填一个再去官网查」：官方接口 → 内置清单 → 批量探测，三级合并。
    // StepFun 实测 /v1/audio/voices 返回 200 但列表为空，所以内置清单 + 探测是主力。
    // 列表用「点击填入」按钮（与对话模型选择器同一模式）——**不能用 datalist**，
    // 浏览器会按输入框已有值过滤选项，导致下拉只剩 1 个甚至 0 个。
    function fillVoiceList(items, defaultVoice, counts, note) {
      const box = document.getElementById("tts-voice-list");
      if (!box) return;
      box.innerHTML = "";
      if (!(items || []).length) {
        box.textContent = t("settings.tts.voices.empty");
        return;
      }
      box.textContent = t("settings.tts.voices.fill");
      (items || []).forEach((v) => {
        const b = document.createElement("button");
        b.className = "btn small";
        b.style.margin = "4px 4px 0 0";
        b.textContent = (v.label && v.label !== v.id ? v.label : v.id)
          + (v.source && v.source !== "api" ? `（${v.source}）` : "");
        b.title = v.id;
        b.onclick = () => {
          const inp = document.getElementById("tts-voice");
          if (inp) inp.value = v.id;
          Toast(t("settings.tts.voices.filled", { id: v.id }));
        };
        box.appendChild(b);
      });
      const meta = document.getElementById("tts-voice-meta");
      if (meta) {
        const c = counts || {};
        meta.textContent = t("settings.tts.voices.meta", {
          n: items.length, api: c.api || 0, builtin: c.builtin || 0, probe: c.probe || 0,
          def: defaultVoice || t("settings.tts.voices.default"),
        }) + (note ? `（${note}）` : "");
      }
    }

    /** 拉取音色。quiet=true 时不弹错误、不落盘表单（用于换端点后的静默刷新）。 */
    async function fetchVoices(opts) {
      opts = opts || {};
      const meta = document.getElementById("tts-voice-meta");
      if (!opts.quiet) {
        try { await pushTtsForm(); }
        catch (e) { Toast(t("settings.tts.err.save", { msg: e.message }), true); return false; }
        if (meta) meta.textContent = t("settings.tts.fetching.voices");
      }
      try {
        const r = await Api.get("/api/tts/voices" + (opts.refresh ? "?refresh=1" : ""));
        fillVoiceList(r.items, r.default_voice, r.source_counts, r.official_error || "");
        return true;
      } catch (e) {
        if (!opts.quiet) Toast(t("settings.tts.err.voices", { msg: e.message }), true);
        return false;
      }
    }

    document.getElementById("tts-models").onclick = async () => {
      const out = document.getElementById("tts-discover-result");
      const list = document.getElementById("tts-model-list");
      try { await pushTtsForm(); }
      catch (e) { out.textContent = "❌ " + t("settings.tts.err.save", { msg: e.message }); return; }
      out.textContent = t("settings.tts.fetching.models");
      try {
        const r = await Api.get("/api/settings/models?target=tts");
        const models = (r.models || []).filter(Boolean);
        if (list) {
          list.innerHTML = "";
          if (!models.length) { list.textContent = t("settings.tts.no_models"); }
          else {
            list.textContent = t("settings.tts.voices.fill");
            models.slice(0, 30).forEach((m) => {
              const b = document.createElement("button");
              b.className = "btn small";
              b.style.margin = "4px 4px 0 0";
              b.textContent = m;
              b.onclick = () => {
                const inp = document.getElementById("tts-model");
                if (inp) inp.value = m;
                Toast(t("settings.tts.models.filled", { m: m }));
              };
              list.appendChild(b);
            });
          }
        }
        out.textContent = models.length
          ? t("settings.tts.models.count", { n: models.length })
          : t("settings.tts.no_models");
      } catch (e) { out.textContent = "❌ " + t("settings.tts.err.models", { msg: e.message }); }
    };

    document.getElementById("tts-voices-fetch").onclick = async () => {
      const out = document.getElementById("tts-discover-result");
      out.textContent = t("settings.tts.fetching.voices");
      const okr = await fetchVoices({});
      out.textContent = okr
        ? t("settings.tts.voices.done")
        : t("settings.tts.voices.failed");
    };

    document.getElementById("tts-voices-probe").onclick = async () => {
      const btn = document.getElementById("tts-voices-probe");
      const out = document.getElementById("tts-discover-result");
      const old = btn.textContent;
      btn.disabled = true; btn.textContent = t("settings.tts.probe.running");
      out.textContent = t("settings.tts.probe.hint");
      try {
        await pushTtsForm();
        const r = await Api.post("/api/tts/voices/probe", { limit: 12 });
        const bad = (r.items || []).filter((x) => !x.ok);
        out.textContent = t("settings.tts.probe.done", { ok: r.ok, n: r.items.length })
          + (r.already ? t("settings.tts.probe.already", { n: r.already }) : "")
          + (r.skipped ? t("settings.tts.probe.skipped", { n: r.skipped }) : "")
          + (bad.length ? t("settings.tts.probe.bad", { id: bad[0].id, err: bad[0].error || "HTTP " + bad[0].http }) : "");
        await fetchVoices({ quiet: true });   // 成功者已进缓存，刷新列表
      } catch (e) {
        out.textContent = "❌ " + t("settings.tts.probe.err", { msg: e.message });
      } finally {
        btn.disabled = false; btn.textContent = old;
      }
    };

    // 换端点后静默刷新音色清单（失败不打扰，可点「拉取音色」重试）
    const ttsBaseEl = document.getElementById("tts-base");
    if (ttsBaseEl) ttsBaseEl.addEventListener("change", () => { fetchVoices({ quiet: true }); });

    document.getElementById("tts-test").onclick = async () => {
      const out = document.getElementById("tts-test-result");
      if (document.getElementById("tts-mode").value !== "cloud") {
        out.textContent = t("settings.tts.test.cloud_only");
        return;
      }
      out.textContent = t("settings.common.testing");
      try {
        // 先把表单里的端点/模型名/Key 提交，否则测的是旧配置
        await pushTtsForm();
        const r = await Api.post("/api/settings/test", { target: "tts" });
        out.textContent = "✅ " + (r.message || t("settings.tts.test.ok"));
      } catch (e) {
        // ApiError 已把封套里的 error.detail 取到 e.detail —— 那是唯一能说明
        // 「到底哪里不对」的信息（端点未配置 / 连不上 / 401 / 404），必须显示。
        const extra = [e.message, e.detail].filter(Boolean).join(" —— ");
        out.textContent = t("settings.tts.test.err", { msg: extra, url: await effectiveTtsUrl() });
      }
    };

    document.getElementById("tts-save").onclick = async () => {
      try {
        await pushTtsForm();
      } catch (e) {
        return Toast(t("settings.err.save_failed", { msg: e.message }), true);
      }
      Toast(t("settings.tts.saved"));
      try { tts = await Api.get("/api/tts/status"); } catch (e) { /* 刷新失败不影响保存 */ }
      if (window.Voice && Voice.syncFromServer) await Voice.syncFromServer();
      refreshTtsLive();   // 刷新「当前生效引擎」
    };

    const dl = document.getElementById("asr-dl");
    if (dl) dl.onclick = () => Toast(t("settings.asr.dl_hint"), true);

    // 试听真正会请求到的端点（脱敏：只拼 base+path，绝不打印 Key）。供用户排查用。
    async function effectiveTtsUrl() {
      const cfg = await Api.get("/api/settings");
      let base = (cfg.tts && cfg.tts.base_url) || "";
      if (!base) base = (cfg.llm && cfg.llm.base_url) || "";
      base = (base || "").trim().replace(/\/+$/, "");
      return base ? t("settings.tts.url.effective", { base: base }) : t("settings.tts.url.none");
    }

    // 试听：三种模式都能在设置页直接听效果，并暴露「实际请求的 URL」让用户自查。
    document.getElementById("tts-preview").onclick = async () => {
      const out = document.getElementById("tts-preview-result");
      const mode = document.getElementById("tts-mode").value;
      const eng = (document.getElementById("tts-engine") || {}).value || "system";
      const SENT = "知伴朗读测试：中文路径本身没有问题，问题在于不要把中文写进批处理文件。";
      if (mode === "off") { out.textContent = t("settings.tts.preview.pick_mode"); return; }
      out.textContent = t("settings.tts.preview.testing");

      if (mode === "cloud") {
        try { await pushTtsForm(); } catch (e) { out.textContent = "❌ " + t("settings.tts.err.save", { msg: e.message }); return; }
        const t0 = performance.now();
        try {
          const resp = await fetch("/api/tts/speech", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: SENT }),
          });
          if (!resp.ok) {
            // 错误封套 {code, message, data, error:{detail}}：上游真实原因在
            // error.detail，两个都显示，用户才能分辨「填错了」还是「端点不支持」。
            let s = "", d = "";
            try {
              const j = (await resp.json()) || {};
              s = j.message || "";
              d = (j.error && j.error.detail) || j.detail || "";
            } catch (e) { /* 非 JSON 响应 */ }
            const extra = [s, d].filter(Boolean).join(" —— ");
            out.textContent = t("settings.tts.preview.cloud.failed", { code: resp.status, extra: extra ? "：" + extra : "" });
            out.textContent += "（" + (await effectiveTtsUrl()) + "）";
            return;
          }
          const mime = (resp.headers.get("content-type") || "audio/mpeg").split(";")[0].trim();
          const blob = await resp.blob();
          const ms = Math.round(performance.now() - t0);
          try { await new Audio(URL.createObjectURL(blob)).play(); } catch (e) { /* 自动播放被拦也照样报元数据 */ }
          out.textContent = `✅ ${t("settings.tts.mode.cloud")} OK：HTTP 200，${mime}，${blob.size} ${en ? "bytes" : "字节"}，${ms} ms`;
          out.textContent += "（" + (await effectiveTtsUrl()) + "）";
        } catch (e) {
          out.textContent = t("settings.tts.preview.cloud.failed2", { msg: e.message });
          out.textContent += "（" + (await effectiveTtsUrl()) + "）";
        }
        return;   // 无论成功失败都不再往下走本地分支
      }

      // local：先提交表单，再按引擎分流
      try { await pushTtsForm(); } catch (e) { /* 本地试听不依赖保存结果 */ }
      if (eng === "melo") {
        try {
          const resp = await fetch("/api/tts/local", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: SENT }),
          });
          if (!resp.ok) { out.textContent = t("settings.tts.preview.melo.failed", { code: resp.status }); return; }
          const blob = await resp.blob();
          try { await new Audio(URL.createObjectURL(blob)).play(); } catch (e) {}
          out.textContent = t("settings.tts.preview.melo.ok", { n: blob.size });
        } catch (e) { out.textContent = t("settings.tts.preview.melo.err", { msg: e.message }); }
        return;
      }
      // system：浏览器系统语音试听（原逻辑）
      if (!window.speechSynthesis) { out.textContent = t("settings.tts.preview.no_synth"); return; }
      const vs = speechSynthesis.getVoices() || [];
      const zh = vs.find((v) => /zh[-_]?CN|cmn|Huihui|Yaoyao|Xiaoxiao|Kangkang|晓晓|慧慧/i.test(v.name + " " + v.lang))
              || vs.find((v) => /^zh/i.test(v.lang));
      const u = new SpeechSynthesisUtterance(SENT);
      u.lang = "zh-CN";
      if (zh) u.voice = zh;
      u.onerror = (e) => { out.textContent = t("settings.tts.preview.play_failed", { reason: (e && e.error) || t("settings.tts.preview.unknown") }); };
      try { speechSynthesis.cancel(); speechSynthesis.speak(u); }
      catch (e) { out.textContent = "❌ " + e.message; return; }
      out.textContent = vs.length
        ? t("settings.tts.preview.system.ok", { n: vs.length, name: zh ? zh.name + "（" + zh.lang + "）" : t("settings.tts.preview.system.no_zh") })
        : t("settings.tts.preview.system.none");
    };

    // 从端点拉取可用模型名：模型名写错/留空是「连得上却问不出话」的头号原因。
    async function pullModels(target, btnId, listId, applyFn) {
      const btn = document.getElementById(btnId);
      const list = document.getElementById(listId);
      const base = val(target === "llm" ? "st-base" : "em-base");
      if (!base && !cfg.llm.base_url) { Toast(t("settings.err.no_base"), true); return; }
      const old = btn.textContent;
      btn.disabled = true; btn.textContent = t("settings.common.testing_short");
      try {
        const patch = target === "llm" ? { llm: { base_url: base } }
          : { embed: { base_url: base, provider: val("em-provider") } };
        const key = val(target === "llm" ? "st-key" : "em-key");
        if (key) patch[target].api_key = key;
        await Api.put("/api/settings", patch);
        const r = await Api.get("/api/settings/models?target=" + target);
        const models = r.models || [];
        list.innerHTML = "";
        if (!models.length) { list.textContent = t("settings.models.empty"); return; }
        list.textContent = t("settings.models.fill");
        models.slice(0, 20).forEach((m) => {
          const b = document.createElement("button");
          b.className = "btn small";
          b.textContent = m;
          b.style.margin = "4px 4px 0 0";
          b.onclick = () => { applyFn(m); Toast(t("settings.models.filled", { m: m })); };
          list.appendChild(b);
        });
      } catch (e) {
        list.innerHTML = "";
        Toast(t("settings.models.err", { msg: e.message }), true);
      } finally { btn.disabled = false; btn.textContent = old; }
    }
    document.getElementById("st-models").onclick = () =>
      pullModels("llm", "st-models", "st-model-list",
        (m) => { document.getElementById("st-model").value = m; });
    document.getElementById("em-models").onclick = () =>
      pullModels("embed", "em-models", "em-model-list",
        (m) => { document.getElementById("em-model").value = m; });
  }

  function val(id) { const n = document.getElementById(id); return n ? n.value.trim() : ""; }
  function updateTTSVis(mode) {
    // 云端四项（端点 / 模型名 / 音色 / 自动发现按钮）只在「云端 API」时出现；其余模式不占位。
    const cloud = mode === "cloud";
    document.getElementById("tts-cloud-row").style.display = cloud ? "" : "none";
    document.getElementById("tts-key-row").style.display = cloud ? "" : "none";
    const disc = document.getElementById("tts-discover-row");
    if (disc) disc.style.display = cloud ? "" : "none";
    const discRes = document.getElementById("tts-discover-result");
    if (discRes) discRes.style.display = cloud ? "" : "none";
    const local = document.getElementById("tts-local-wrap");
    if (local) local.style.display = mode === "local" ? "" : "none";
    const cus = document.getElementById("tts-custom-wrap");
    if (cus) cus.style.display = mode === "custom" ? "" : "none";
    updateEngineHint();
  }

  /** 本地引擎说明：模型没下载时明确告知，避免选了 melo 却没声音。 */
  function updateEngineHint() {
    const hint = document.getElementById("tts-engine-hint");
    if (!hint) return;
    const sel = document.getElementById("tts-engine");
    if (!sel) return;
    if (sel.value === "melo") {
      if (TtsModelReady) {
        hint.textContent = t("settings.tts.hint.melo.ready");
        hint.style.color = "";
      } else {
        hint.textContent = t("settings.tts.hint.melo.missing");
        hint.style.color = "var(--bad)";
      }
    } else {
      hint.textContent = t("settings.tts.hint.system");
      hint.style.color = "";
    }
  }
  async function saveIfChanged() {
    const base = val("st-base"), model = val("st-model"), key = val("st-key");
    const patch = { llm: { base_url: base, model: model } };
    if (key) patch.llm.api_key = key;
    await Api.put("/api/settings", patch);
  }

  window.Views = window.Views || {};
  window.Views.settings = { render };
})();
