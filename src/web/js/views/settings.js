/* 设置页：模型与 API / 语音（ASR 本地 + TTS 三态）。 */
(function () {
  "use strict";

  const PRESETS = [
    ["https://api.deepseek.com/v1", "deepseek-chat", "DeepSeek"],
    ["https://open.bigmodel.cn/api/paas/v4", "glm-4-plus", "智谱 GLM"],
    ["https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "阿里百炼"],
    ["https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct", "SiliconFlow"],
    ["http://127.0.0.1:11434/v1", "", "Ollama（本地）"],
  ];

  // 本地 MeloTTS 模型是否就位（来自 /api/tts/status，设置页提示与降级判断用）。
  let TtsModelReady = false;

  async function render(host) {
    const cfg = await Api.get("/api/settings");
    const asr = await Api.get("/api/asr/status");
    let tts = await Api.get("/api/tts/status");
    TtsModelReady = !!(tts && tts.local_model_available);

    host.innerHTML = "";
    const page = document.createElement("div");
    page.className = "page";
    page.innerHTML = `
      <div class="card">
        <h3 style="margin:0 0 4px">对话模型</h3>
        <p class="hint" style="margin-top:0">OpenAI 兼容端点均可<strong>直接手填</strong>，不必先选预设。<strong>接口地址与模型名两项都必须填</strong>，否则无法提问；填好后点「测试连接」确认可用。</p>
        <div class="field"><label>快速填充</label>
          <select id="st-preset"><option value="">— 选择服务商 —</option>
            ${PRESETS.map((p, i) => `<option value="${i}">${p[2]}</option>`).join("")}
            <option value="custom">自定义（手动填写）</option>
          </select></div>
        <div class="row">
          <div class="field"><label>base_url</label><input type="text" id="st-base" value="${cfg.llm.base_url}" placeholder="https://api.deepseek.com/v1"></div>
          <div class="field" style="max-width:280px"><label>模型名</label>
            <input type="text" id="st-model" value="${cfg.llm.model}" placeholder="deepseek-chat">
            <button class="btn small" id="st-models" style="margin-top:6px">拉取可用模型</button>
            <div id="st-model-list" class="hint" style="margin-top:6px"></div>
          </div>
        </div>
        <div class="row">
          <div class="field"><label>API Key（本地加密存储，不会写入日志或下发到界面）</label>
            <input type="password" id="st-key" placeholder="${cfg.llm.api_key_set ? "已配置（" + cfg.llm.api_key_masked + "），留空则不修改" : "sk-..."}"></div>
          <div class="field" style="max-width:120px"><label>温度</label><input type="text" id="st-temp" value="${cfg.llm.temperature}"></div>
          <div class="field" style="max-width:140px"><label>最大 tokens</label><input type="text" id="st-maxtok" value="${cfg.llm.max_tokens}"></div>
        </div>
        <div class="field">
          <label>深度思考</label>
          <label class="switch"><input type="checkbox" id="st-think"${cfg.llm.enable_thinking ? " checked" : ""}> 开启深度思考模式</label>
          <div class="hint" style="margin-top:6px">适用于 Qwen3 等「混合推理」模型：开启后模型回答前会先推理，更细致但也更慢。DeepSeek-R1 等推理模型自身就会思考，无需开启；若你的服务商不认识该参数会报 HTTP 400，关掉即可。</div>
        </div>
        <div class="row">
          <button class="btn primary" id="st-save">保存</button>
          <button class="btn" id="st-test">测试连接</button>
          <span class="hint" id="st-test-result"></span>
        </div>
        <p class="hint">断网使用：启动本机 Ollama 后，base_url 填 <code>http://127.0.0.1:11434/v1</code>，Key 留空，即可完全离线问答。</p>
      </div>

      <div class="card">
        <h3 style="margin:0 0 4px">嵌入模型（用于语义检索）</h3>
        <p class="hint" style="margin-top:0">
          自动（<code>auto</code>）：配了云端就用云端，连不上自动退回本地；本地（<code>local</code>）：只用本地引擎检索（完全离线）；云端（<code>cloud</code>）：只用云端。
          <br>不配置也能用 —— 自动退化为本地检索。base_url 留空时<strong>沿用对话模型的地址</strong>，Key 留空时沿用对话模型的 Key。
          <br>⚠️ 换过嵌入模型后，旧文档需要用新模型重建索引：在「工作台 → 资料库」里对文档点 <strong>↻</strong> 重新解析。
        </p>
        <div class="row">
          <div class="field" style="max-width:260px"><label>检索方式</label>
            <select id="em-provider">
              ${[["auto", "自动（auto）— 配了云端用云端，连不上退回本地"],
                 ["cloud", "云端（cloud）— 只用云端嵌入"],
                 ["local", "本地（local）— 完全离线"]]
                .map(([v, n]) => `<option value="${v}" ${cfg.embed.provider === v ? "selected" : ""}>${n}</option>`).join("")}
            </select></div>
          <div class="field"><label>base_url（可留空，与对话模型相同）</label><input type="text" id="em-base" value="${cfg.embed.base_url}"></div>
          <div class="field"><label>模型名</label><input type="text" id="em-model" value="${cfg.embed.model}" placeholder="text-embedding-3-small"></div>
        </div>
        <div class="row">
          <div class="field" style="max-width:220px"><label>本地引擎（默认 hash；bge = 本地语义模型）</label>
            <select id="em-local-engine">
              <option value="hash" ${cfg.embed.local_engine !== "bge" ? "selected" : ""}>hash — 字符哈希（零下载，默认）</option>
              <option value="bge" ${cfg.embed.local_engine === "bge" ? "selected" : ""}>bge — 语义模型（约 24MB，默认关闭）</option>
            </select>
            <div class="hint" id="em-bge-status" style="margin-top:4px"></div>
          </div>
          <div class="field"><label>本地模型目录（相对随包 models/ 或绝对路径）</label><input type="text" id="em-local-dir" value="${cfg.embed.local_dir || "models/embed/bge-small-zh-v1.5"}"></div>
        </div>
        <div class="row">
          <button class="btn small" id="em-models">拉取可用模型</button>
          <div id="em-model-list" class="hint"></div>
        </div>
        <div class="row">
          <div class="field"><label>嵌入 API Key（留空则沿用对话模型 Key）</label><input type="password" id="em-key" placeholder="${cfg.embed.api_key_set ? "已配置，留空则不修改" : ""}"></div>
          <button class="btn primary" id="em-save">保存嵌入配置</button>
        </div>
      </div>

      <div class="card">
        <h3 style="margin:0 0 4px">语音</h3>
        <div class="field">
          <label>语音输入（ASR）— 固定本地识别，不调用任何云端服务</label>
          <div class="row">
            <span class="pill ${asr.available ? "ok" : "bad"}">模型${asr.available ? "已就绪" : "未下载"}</span>
            <span class="pill">${asr.loaded ? "已加载" : "按需加载"}</span>
            <span class="hint">首次使用会自动下载模型（约 228MB），之后完全离线可用。</span>
            ${asr.available ? "" : '<button class="btn small" id="asr-dl">下载语音模型</button>'}
          </div>
        </div>
        <div class="field">
          <label>朗读（TTS）— 默认关闭</label>
          <div class="row">
            <select id="tts-mode" class="tts-mode-sel" style="padding:8px 10px;border:1px solid var(--border);border-radius:8px">
              ${[["off", "关闭"], ["local", "本地朗读（离线，零外发）"], ["cloud", "云端 API"]]
                .map(([v, n]) => `<option value="${v}" ${tts.mode === v ? "selected" : ""}>${n}</option>`).join("")}
            </select>
            <div class="field" id="tts-local-wrap">
              <label>本地引擎</label>
              <select id="tts-engine" style="padding:8px 10px;border:1px solid var(--border);border-radius:8px">
                ${[["system", "系统语音（零依赖，音色较机械）"], ["melo", "神经语音 MeloTTS（更自然，中英混读）"]]
                  .map(([v, n]) => `<option value="${v}" ${(cfg.tts.local_engine || "system") === v ? "selected" : ""}>${n}</option>`).join("")}
              </select>
              <span class="hint" id="tts-engine-hint"></span>
            </div>
          </div>
          <div class="row" id="tts-cloud-row">
            <div class="field" id="tts-cloud-wrap"><label>语音端点 base_url</label><input type="text" id="tts-base" value="${cfg.tts.base_url}" placeholder="留空 = 沿用对话模型端点"></div>
            <div class="field" id="tts-cloud-model"><label>语音模型名</label><input type="text" id="tts-model" value="${cfg.tts.model}" placeholder="FunAudioLLM/SpeechT5/TTS"></div>
            <div class="field" id="tts-voice-wrap"><label>音色 voice</label><input type="text" id="tts-voice" value="${cfg.tts.voice || ""}" placeholder="留空=服务商默认"><span class="hint" id="tts-voice-meta"></span></div>
          </div>
          <div class="row" id="tts-discover-row">
            <button class="btn small" id="tts-models">拉取模型</button>
            <button class="btn small" id="tts-voices-fetch">拉取音色</button>
            <button class="btn small" id="tts-voices-probe">探测可用音色</button>
          </div>
          <p class="hint" id="tts-discover-result"></p>
          <!-- 列表用「点击填入」按钮（与对话模型选择器同一模式）。
               不能用 <datalist>：浏览器会按输入框已有值**过滤**选项 ——
               模型名里已有 stepaudio-2.5-tts 时 9 个模型只剩 1 个可选，
               音色里是 livelybreezy-female 时 8 个内置音色一个都不显示。 -->
          <div id="tts-model-list" class="hint"></div>
          <div id="tts-voice-list" class="hint"></div>
          <div class="row" id="tts-key-row">
            <div class="field"><label>语音 Key（留空则沿用对话模型 Key）</label><input type="password" id="tts-key" placeholder="${cfg.tts.api_key_set ? "已配置，留空则不修改" : "与对话模型同一站点时可留空"}"></div>
          </div>
          <p class="hint" id="tts-live"></p>
          <div class="row">
            <button class="btn" id="tts-test">测试连接</button>
            <button class="btn primary" id="tts-save">保存语音设置</button>
            <button class="btn" id="tts-preview">🔊 试听</button>
          </div>
          <!-- 结果单独成行：文案很长（含 HTTP 状态与实际请求 URL），
               塞在按钮同一行会把按钮挤到换行，导致「保存语音设/置」这种断词。 -->
          <p class="hint" id="tts-test-result"></p>
          <p class="hint" id="tts-preview-result"></p>
          <p class="hint">语音端点可与对话模型<strong>不同</strong>：<strong>同一个站点</strong>时端点与 Key <strong>都可以留空</strong>，程序自动沿用对话模型的地址与 Key（<strong>不需要另外申请第二个 Key</strong>）；<strong>换成别的语音站点</strong>时，才需要填该站点的地址与那家站点自己的 Key（此时不会误用对话模型的 Key）。本地朗读由浏览器调用 Windows 系统语音（如 Microsoft Huihui），完全离线、零 Key。开关在工作台底部；开启后每条新回答都会朗读。</p>
        </div>
      </div>`;
    host.appendChild(page);

    // 预设
    document.getElementById("st-preset").onchange = (e) => {
      if (e.target.value === "custom") {
        // 自定义：清空三项并聚焦地址，方便直接粘贴自己的服务商地址。
        ["st-base", "st-model", "st-key"].forEach((id) => {
          document.getElementById(id).value = "";
        });
        const baseEl = document.getElementById("st-base");
        baseEl.placeholder = "https://你的服务商地址/v1";
        baseEl.focus();
        return;
      }
      const p = PRESETS[e.target.value];
      if (!p) return;
      document.getElementById("st-base").value = p[0];
      document.getElementById("st-model").value = p[1] || "";
      if (p[2].indexOf("Ollama") === 0) {
        document.getElementById("st-key").value = "";
        Toast("本地 Ollama 还需填模型名：用 ollama list 查看后填入，如 qwen2.5:7b", true);
      }
    };
    document.getElementById("st-save").onclick = async () => {
      const base = val("st-base");
      const model = val("st-model");
      // 只填地址不填模型名 = 无法提问；必须在保存时就说清楚，不能静默通过。
      if (!base) { document.getElementById("st-base").focus(); return Toast("请先填写接口地址 base_url", true); }
      if (!model) { document.getElementById("st-model").focus(); return Toast("请先填写模型名（如 deepseek-chat）", true); }
      const patch = {
        llm: {
          base_url: base, model: model,
          temperature: parseFloat(val("st-temp")) || 0.7,
          max_tokens: parseInt(val("st-maxtok"), 10) || 2048,
          enable_thinking: !!(document.getElementById("st-think") || {}).checked,
        },
      };
      const key = val("st-key");
      if (key) patch.llm.api_key = key;
      await Api.put("/api/settings", patch);
      Toast("已保存"); Main.refreshModelBadge();
    };
    document.getElementById("st-test").onclick = async () => {
      const out = document.getElementById("st-test-result");
      const base = val("st-base"), model = val("st-model");
      if (!base) { document.getElementById("st-base").focus(); return Toast("请先填写接口地址 base_url", true); }
      if (!model) { document.getElementById("st-model").focus(); return Toast("请先填写模型名（如 deepseek-chat）", true); }
      out.textContent = "测试中…";
      await saveIfChanged();
      try {
        const r = await Api.post("/api/settings/test", { target: "llm" });
        out.textContent = `✅ 连接成功（${r.latency_ms}ms）` + (r.warning ? " · ⚠️ " + r.warning : "");
        if (r.warning) Toast(r.warning, true);
        Main.refreshModelBadge();
      } catch (e) { out.textContent = "❌ " + e.message; }
    };

    document.getElementById("em-save").onclick = async () => {
      const provider = val("em-provider");
      const base = val("em-base"), model = val("em-model");
      if (provider !== "local" && !base && !cfg.llm.base_url) {
        return Toast("嵌入端点未配置：请填 base_url，或先配置上面的对话模型地址", true);
      }
      if (provider === "cloud" && !model) {
        document.getElementById("em-model").focus();
        return Toast("云端嵌入必须填写模型名（如 BAAI/bge-m3）", true);
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
        Toast("已保存。未填模型名 → 搜索时退化为本地关键词检索", true);
      } else {
        Toast("嵌入配置已保存");
      }
    };

    // bge 模型就绪状态（只读展示；下载用 scripts/download_bge_model.py）
    Api.get("/api/settings/embed/status").then((st) => {
      const el2 = document.getElementById("em-bge-status");
      if (el2 && st) {
        el2.textContent = st.bge_available
          ? "✅ bge 模型已就绪（" + (st.bge_dir || "") + "）"
          : "bge 模型未下载：运行 scripts/download_bge_model.py（约 24MB）；未就绪时自动回退 hash，不影响使用";
      }
    }).catch(() => { /* 状态拿不到就不显示 */ });

    document.getElementById("tts-mode").onchange = (e) => updateTTSVis(e.target.value);
    updateTTSVis(cfg.tts.mode);
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
      let txt = "当前生效：已关闭";
      if (tts.enabled && tts.mode === "cloud") {
        txt = tts.cloud_configured ? "当前生效：云端朗读" : "当前生效：无 —— 云端尚未配置（端点或模型名为空）";
      } else if (tts.enabled && tts.mode === "local") {
        if ((tts.local_engine || "system") === "melo" && tts.local_model_available) {
          txt = "当前生效：本地神经语音 MeloTTS";
        } else {
          txt = "当前生效：Windows 系统语音（浏览器合成）";
        }
      }
      live.textContent = txt;
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
        box.textContent = "未发现可用音色 —— 点「探测可用音色」试一试。";
        return;
      }
      box.textContent = "点击填入：";
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
          Toast("已填入音色 " + v.id);
        };
        box.appendChild(b);
      });
      const meta = document.getElementById("tts-voice-meta");
      if (meta) {
        const c = counts || {};
        meta.textContent = `音色 ${items.length} 个 · 来源：api ${c.api || 0} / 内置 ${c.builtin || 0}`
          + ` / 探测 ${c.probe || 0} · 默认：${defaultVoice || "服务商默认"}` + (note ? `（${note}）` : "");
      }
    }

    /** 拉取音色。quiet=true 时不弹错误、不落盘表单（用于换端点后的静默刷新）。 */
    async function fetchVoices(opts) {
      opts = opts || {};
      const meta = document.getElementById("tts-voice-meta");
      if (!opts.quiet) {
        try { await pushTtsForm(); }
        catch (e) { Toast("保存配置失败：" + e.message, true); return false; }
        if (meta) meta.textContent = "拉取音色中…";
      }
      try {
        const r = await Api.get("/api/tts/voices" + (opts.refresh ? "?refresh=1" : ""));
        fillVoiceList(r.items, r.default_voice, r.source_counts, r.official_error || "");
        return true;
      } catch (e) {
        if (!opts.quiet) Toast("拉取音色失败：" + e.message, true);
        return false;
      }
    }

    document.getElementById("tts-models").onclick = async () => {
      const out = document.getElementById("tts-discover-result");
      const list = document.getElementById("tts-model-list");
      try { await pushTtsForm(); }
      catch (e) { out.textContent = "❌ 保存配置失败：" + e.message; return; }
      out.textContent = "拉取模型中…";
      try {
        const r = await Api.get("/api/settings/models?target=tts");
        const models = (r.models || []).filter(Boolean);
        if (list) {
          list.innerHTML = "";
          if (!models.length) { list.textContent = "端点未返回模型列表，请手动填写模型名。"; }
          else {
            list.textContent = "点击填入：";
            models.slice(0, 30).forEach((m) => {
              const b = document.createElement("button");
              b.className = "btn small";
              b.style.margin = "4px 4px 0 0";
              b.textContent = m;
              b.onclick = () => {
                const inp = document.getElementById("tts-model");
                if (inp) inp.value = m;
                Toast("已填入模型 " + m);
              };
              list.appendChild(b);
            });
          }
        }
        out.textContent = models.length
          ? `端点返回 ${models.length} 个模型 —— 点下面的模型名即可填入`
          : "端点未返回模型列表，请手动填写模型名。";
      } catch (e) { out.textContent = "❌ 拉取模型失败：" + e.message; }
    };

    document.getElementById("tts-voices-fetch").onclick = async () => {
      const out = document.getElementById("tts-discover-result");
      out.textContent = "拉取音色中…";
      const okr = await fetchVoices({});
      out.textContent = okr
        ? "已拉取音色 —— 点下面的音色名即可填入"
        : "拉取失败，详见音色栏提示";
    };

    document.getElementById("tts-voices-probe").onclick = async () => {
      const btn = document.getElementById("tts-voices-probe");
      const out = document.getElementById("tts-discover-result");
      const old = btn.textContent;
      btn.disabled = true; btn.textContent = "探测中…";
      out.textContent = "探测中…（对每个候选音色发一次短合成，会消耗少量额度）";
      try {
        await pushTtsForm();
        const r = await Api.post("/api/tts/voices/probe", { limit: 12 });
        const bad = (r.items || []).filter((x) => !x.ok);
        out.textContent = `探测完成：本次可用 ${r.ok} / 共 ${r.items.length}`
          + (r.already ? `（此前已确认 ${r.already} 个）` : "")
          + (r.skipped ? `（还有 ${r.skipped} 个未测，再点一次继续）` : "")
          + (bad.length ? `；不可用示例：${bad[0].id}（${bad[0].error || "HTTP " + bad[0].http}）` : "");
        await fetchVoices({ quiet: true });   // 成功者已进缓存，刷新列表
      } catch (e) {
        out.textContent = "❌ 探测失败：" + e.message;
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
        out.textContent = "仅「云端 API」模式需要测试连接";
        return;
      }
      out.textContent = "测试中…";
      try {
        // 先把表单里的端点/模型名/Key 提交，否则测的是旧配置
        await pushTtsForm();
        const r = await Api.post("/api/settings/test", { target: "tts" });
        out.textContent = "✅ " + (r.message || "端点可用");
      } catch (e) {
        // ApiError 已把封套里的 error.detail 取到 e.detail —— 那是唯一能说明
        // 「到底哪里不对」的信息（端点未配置 / 连不上 / 401 / 404），必须显示。
        const extra = [e.message, e.detail].filter(Boolean).join(" —— ");
        out.textContent = "❌ " + extra + "（" + (await effectiveTtsUrl()) + "）";
      }
    };

    document.getElementById("tts-save").onclick = async () => {
      try {
        await pushTtsForm();
      } catch (e) {
        return Toast("保存失败：" + e.message, true);
      }
      Toast("语音设置已保存");
      try { tts = await Api.get("/api/tts/status"); } catch (e) { /* 刷新失败不影响保存 */ }
      if (window.Voice && Voice.syncFromServer) await Voice.syncFromServer();
      refreshTtsLive();   // 刷新「当前生效引擎」
    };

    const dl = document.getElementById("asr-dl");
    if (dl) dl.onclick = () => Toast("请在软件目录运行 scripts/download_models.ps1 下载语音模型", true);

    // 试听真正会请求到的端点（脱敏：只拼 base+path，绝不打印 Key）。供用户排查用。
    async function effectiveTtsUrl() {
      const cfg = await Api.get("/api/settings");
      let base = (cfg.tts && cfg.tts.base_url) || "";
      if (!base) base = (cfg.llm && cfg.llm.base_url) || "";
      base = (base || "").trim().replace(/\/+$/, "");
      return base ? `实际请求：${base}/audio/speech` : "实际请求：未配置";
    }

    // 试听：三种模式都能在设置页直接听效果，并暴露「实际请求的 URL」让用户自查。
    document.getElementById("tts-preview").onclick = async () => {
      const out = document.getElementById("tts-preview-result");
      const mode = document.getElementById("tts-mode").value;
      const eng = (document.getElementById("tts-engine") || {}).value || "system";
      const SENT = "知伴朗读测试：中文路径本身没有问题，问题在于不要把中文写进批处理文件。";
      if (mode === "off") { out.textContent = "请先把朗读模式选为「本地朗读」或「云端 API」"; return; }
      out.textContent = "试听中…";

      if (mode === "cloud") {
        try { await pushTtsForm(); } catch (e) { out.textContent = "❌ 保存配置失败：" + e.message; return; }
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
            out.textContent = "❌ 云端合成失败：HTTP " + resp.status + (extra ? "：" + extra : "");
            out.textContent += "（" + (await effectiveTtsUrl()) + "）";
            return;
          }
          const mime = (resp.headers.get("content-type") || "audio/mpeg").split(";")[0].trim();
          const blob = await resp.blob();
          const ms = Math.round(performance.now() - t0);
          try { await new Audio(URL.createObjectURL(blob)).play(); } catch (e) { /* 自动播放被拦也照样报元数据 */ }
          out.textContent = `✅ 云端合成 OK：HTTP 200，${mime}，${blob.size} 字节，${ms} ms`;
          out.textContent += "（" + (await effectiveTtsUrl()) + "）";
        } catch (e) {
          out.textContent = "❌ 云端合成失败（连不上后端或端点）：" + e.message;
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
          if (!resp.ok) { out.textContent = "❌ 本地神经语音合成失败：HTTP " + resp.status; return; }
          const blob = await resp.blob();
          try { await new Audio(URL.createObjectURL(blob)).play(); } catch (e) {}
          out.textContent = `✅ 本地神经语音 OK：${blob.size} 字节（完全离线）`;
        } catch (e) { out.textContent = "❌ 本地神经语音失败：" + e.message; }
        return;
      }
      // system：浏览器系统语音试听（原逻辑）
      if (!window.speechSynthesis) { out.textContent = "❌ 当前浏览器不支持本地朗读"; return; }
      const vs = speechSynthesis.getVoices() || [];
      const zh = vs.find((v) => /zh[-_]?CN|cmn|Huihui|Yaoyao|Xiaoxiao|Kangkang|晓晓|慧慧/i.test(v.name + " " + v.lang))
              || vs.find((v) => /^zh/i.test(v.lang));
      const u = new SpeechSynthesisUtterance(SENT);
      u.lang = "zh-CN";
      if (zh) u.voice = zh;
      u.onerror = (e) => { out.textContent = "❌ 播放失败：" + ((e && e.error) || "未知原因"); };
      try { speechSynthesis.cancel(); speechSynthesis.speak(u); }
      catch (e) { out.textContent = "❌ " + e.message; return; }
      out.textContent = vs.length
        ? `共 ${vs.length} 个音色，使用：${zh ? zh.name + "（" + zh.lang + "）" : "系统默认 ⚠️ 未找到中文音色"}`
        : "未检测到音色（首次调用可能为空，请再点一次）";
    };

    // 从端点拉取可用模型名：模型名写错/留空是「连得上却问不出话」的头号原因。
    async function pullModels(target, btnId, listId, applyFn) {
      const btn = document.getElementById(btnId);
      const list = document.getElementById(listId);
      const base = val(target === "llm" ? "st-base" : "em-base");
      if (!base && !cfg.llm.base_url) { Toast("请先填写接口地址 base_url", true); return; }
      const old = btn.textContent;
      btn.disabled = true; btn.textContent = "获取中…";
      try {
        const patch = target === "llm" ? { llm: { base_url: base } }
          : { embed: { base_url: base, provider: val("em-provider") } };
        const key = val(target === "llm" ? "st-key" : "em-key");
        if (key) patch[target].api_key = key;
        await Api.put("/api/settings", patch);
        const r = await Api.get("/api/settings/models?target=" + target);
        const models = r.models || [];
        list.innerHTML = "";
        if (!models.length) { list.textContent = "端点未返回模型列表，请手动填写模型名。"; return; }
        list.textContent = "点击填入：";
        models.slice(0, 20).forEach((m) => {
          const b = document.createElement("button");
          b.className = "btn small";
          b.textContent = m;
          b.style.margin = "4px 4px 0 0";
          b.onclick = () => { applyFn(m); Toast("已填入 " + m); };
          list.appendChild(b);
        });
      } catch (e) {
        list.innerHTML = "";
        Toast("获取模型列表失败：" + e.message, true);
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
        hint.textContent = "MeloTTS 模型已就绪（中文+英文混读，本地推理、零外发）。";
        hint.style.color = "";
      } else {
        hint.textContent = "⚠ 模型未下载：请运行 scripts/download_tts_model.py，"
          + "或改用「系统语音」。未下载时会自动回退系统语音。";
        hint.style.color = "var(--bad)";
      }
    } else {
      hint.textContent = "用 Windows 自带音色，无需下载任何模型。";
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
