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
    const tts = await Api.get("/api/tts/status");
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
          <code>auto</code>：配了云端就用云端，连不上自动退回本地；<code>local</code>：只用本地关键词检索（完全离线）；<code>cloud</code>：只用云端。
          <br>不配置也能用 —— 自动退化为本地检索。base_url 留空时<strong>沿用对话模型的地址</strong>，Key 留空时沿用对话模型的 Key。
          <br>⚠️ 换过嵌入模型后，旧文档需要用新模型重建索引：在「工作台 → 资料库」里对文档点 <strong>↻</strong> 重新解析。
        </p>
        <div class="row">
          <div class="field" style="max-width:160px"><label>provider</label>
            <select id="em-provider">
              ${["auto", "cloud", "local"].map((p) => `<option value="${p}" ${cfg.embed.provider === p ? "selected" : ""}>${p}</option>`).join("")}
            </select></div>
          <div class="field"><label>base_url（可留空，与对话模型相同）</label><input type="text" id="em-base" value="${cfg.embed.base_url}"></div>
          <div class="field"><label>模型名</label><input type="text" id="em-model" value="${cfg.embed.model}" placeholder="text-embedding-3-small"></div>
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
            <select id="tts-mode" style="padding:8px 10px;border:1px solid var(--border);border-radius:8px">
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
            <div class="field" id="tts-cloud-wrap"><label>base_url</label><input type="text" id="tts-base" value="${cfg.tts.base_url}" placeholder="https://api.siliconflow.cn/v1"></div>
            <div class="field" id="tts-cloud-model"><label>模型名</label><input type="text" id="tts-model" value="${cfg.tts.model}" placeholder="FunAudioLLM/SpeechT5/TTS"></div>
          </div>
          <div class="row">
            <div class="field"><label>语音 Key（与对话模型分开）</label><input type="password" id="tts-key" placeholder="${cfg.tts.api_key_set ? "已配置，留空则不修改" : ""}"></div>
            <button class="btn primary" id="tts-save">保存语音设置</button>
            <button class="btn" id="tts-preview">🔊 试听</button>
            <span class="hint" id="tts-preview-result"></span>
          </div>
          <p class="hint">本地朗读由浏览器调用 Windows 系统语音（如 Microsoft Huihui），完全离线、零 Key。开关在工作台底部；开启后**每条新回答都会朗读**。</p>
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
      const patch = { embed: { provider: provider, base_url: base, model: model } };
      const key = val("em-key");
      if (key) patch.embed.api_key = key;
      await Api.put("/api/settings", patch);
      if (provider !== "local" && !model) {
        Toast("已保存。未填模型名 → 搜索时退化为本地关键词检索", true);
      } else {
        Toast("嵌入配置已保存");
      }
    };

    document.getElementById("tts-mode").onchange = (e) => updateTTSVis(e.target.value);
    updateTTSVis(cfg.tts.mode);
    const engSel = document.getElementById("tts-engine");
    if (engSel) engSel.onchange = updateEngineHint;
    document.getElementById("tts-save").onclick = async () => {
      const mode = document.getElementById("tts-mode").value;
      const patch = {
        tts: {
          enabled: mode !== "off", mode,
          base_url: val("tts-base"), model: val("tts-model"),
          local_engine: engSel ? engSel.value : "system",
        },
      };
      const key = val("tts-key");
      if (key) patch.tts.api_key = key;
      await Api.put("/api/settings", patch);
      Toast("语音设置已保存");
    };

    const dl = document.getElementById("asr-dl");
    if (dl) dl.onclick = () => Toast("请在软件目录运行 scripts/download_models.ps1 下载语音模型", true);

    // 试听：一键判断「本机到底有没有可用的中文系统语音」，省去来回试。
    document.getElementById("tts-preview").onclick = () => {
      const out = document.getElementById("tts-preview-result");
      const mode = document.getElementById("tts-mode").value;
      if (mode === "off") { out.textContent = "请先把朗读模式选为「本地系统语音」"; return; }
      if (mode === "cloud") { out.textContent = "云端朗读在工作台播放，设置页不试听"; return; }
      if (!window.speechSynthesis) { out.textContent = "❌ 当前浏览器不支持本地朗读"; return; }
      const vs = speechSynthesis.getVoices() || [];
      const zh = vs.find((v) => /zh[-_]?CN|cmn|Huihui|Yaoyao|Xiaoxiao|Kangkang|晓晓|慧慧/i.test(v.name + " " + v.lang))
              || vs.find((v) => /^zh/i.test(v.lang));
      const u = new SpeechSynthesisUtterance(
        "知伴朗读测试：中文路径本身没有问题，问题在于不要把中文写进批处理文件。");
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
    const show = mode === "cloud";
    document.getElementById("tts-cloud-wrap").style.display = show ? "" : "none";
    document.getElementById("tts-cloud-model").style.display = show ? "" : "none";
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
