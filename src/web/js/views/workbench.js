/* 主工作台：资料库 + 对话（流式/引导式/语音输入）+ 引用与记忆面板。 */
(function () {
  "use strict";

  const S = {
    documents: [],
    conversations: [],
    conversationId: null,
    messages: [],
    selectedDocs: [],      // 选中的材料 id
    guided: false,
    grounding: "strict",
    citations: [],
    memories: [],
    streaming: false,
    abort: null,
    recorder: null,
    recording: false,
    tts: null,
    ttsOn: false,          // 朗读开关（跨重渲染保持；新回答到达时据此朗读）
  };

  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };

  /* ── 数据加载 ─────────────────────────── */
  async function loadDocuments() {
    const d = await Api.get("/api/documents?page=1&page_size=200");
    S.documents = d.items || [];
  }
  async function loadConversations() {
    S.conversations = await Api.get("/api/conversations");
  }
  async function loadMemories() {
    try {
      const d = await Api.get("/api/memories?page=1&page_size=6");
      S.memories = d.items || [];
    } catch (e) { S.memories = []; }
  }
  async function loadTTS() {
    try { S.tts = await Api.get("/api/tts/status"); } catch (e) { S.tts = null; }
  }

  async function openConversation(id) {
    S.conversationId = id;
    const conv = await Api.get("/api/conversations/" + id);
    S.messages = conv.messages || [];
    S.citations = [];
    S.guided = conv.mode === "guided";
    S.selectedDocs = conv.document_ids || [];
    renderChat();
    renderSide();
    renderRight();
  }

  /* ── 渲染 ─────────────────────────────── */
  function renderSide() {
    const docs = document.getElementById("side-docs");
    docs.innerHTML = "";
    const head = el("div", "panel-head",
      `<span>资料库（${S.documents.length}）</span><button class="btn small primary" id="btn-upload">＋ 上传</button>`);
    docs.appendChild(head);
    const body = el("div", "panel-body");
    if (!S.documents.length) {
      body.appendChild(el("div", "empty", "还没有资料。<br>上传 PDF / DOCX / PPTX / MD / TXT 后即可基于材料提问。"));
    }
    S.documents.forEach((d) => {
      const item = el("div", "item" + (S.selectedDocs.includes(d.id) ? " active" : ""));
      const icon = { pdf: "📕", docx: "📘", pptx: "📙", md: "📗", txt: "📒", html: "🌐" }[d.fmt] || "📄";
      item.appendChild(el("span", null, icon));
      item.appendChild(el("span", "t", `${d.title}`));
      item.appendChild(el("small", null, d.status === "ready" ? `${d.page_count || ""}` : d.status));
      const re = el("button", "r", "↻");
      re.title = "用当前嵌入模型重新解析（换过嵌入模型后需点这里重建索引）";
      re.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`重新解析《${d.title}》？\n\n将用当前嵌入模型重建切片与向量索引，文档较大时耗时较久。`)) return;
        re.textContent = "…"; re.disabled = true;
        try {
          const r = await Api.post("/api/documents/" + d.id + "/reparse");
          const doc = (r && r.document) || {};
          if (doc.status === "failed") Toast("重新解析失败：" + (doc.error || "未知原因"), true);
          else if (doc.warning) Toast(doc.warning, true);
          else Toast(`《${d.title}》已重新解析，索引已重建`);
        } catch (e) {
          Toast("重新解析失败：" + e.message, true);
        } finally {
          await loadDocuments(); renderSide();
        }
      };
      item.appendChild(re);
      const del = el("button", "x", "✕");
      del.title = "删除";
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除《${d.title}》？其切片与索引会一并删除。`)) return;
        await Api.del("/api/documents/" + d.id);
        await loadDocuments(); renderSide();
      };
      item.appendChild(del);
      item.onclick = () => {
        const i = S.selectedDocs.indexOf(d.id);
        if (i >= 0) S.selectedDocs.splice(i, 1); else S.selectedDocs.push(d.id);
        renderSide();
      };
      item.title = d.warning || d.title;
      body.appendChild(item);
    });
    docs.appendChild(body);

    const convs = document.getElementById("side-convs");
    convs.innerHTML = "";
    convs.appendChild(el("div", "panel-head",
      `<span>会话</span><button class="btn small" id="btn-newconv">＋ 新建</button>`));
    const cbody = el("div", "panel-body");
    S.conversations.forEach((c) => {
      const item = el("div", "item" + (c.id === S.conversationId ? " active" : ""));
      item.appendChild(el("span", "t", `${c.mode === "guided" ? "🎓 " : ""}${c.title || "新的会话"}`));
      const del = el("button", "x", "✕");
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm("删除该会话？")) return;
        await Api.del("/api/conversations/" + c.id);
        if (S.conversationId === c.id) { S.conversationId = null; S.messages = []; renderChat(); }
        await loadConversations(); renderSide();
      };
      item.appendChild(del);
      item.onclick = () => openConversation(c.id);
      cbody.appendChild(item);
    });
    convs.appendChild(cbody);

    document.getElementById("btn-upload").onclick = triggerUpload;
    document.getElementById("btn-newconv").onclick = newConversation;
  }

  function renderChat() {
    const sc = document.getElementById("chat-scroll");
    if (!sc) return;
    sc.innerHTML = "";
    if (!S.messages.length) {
      sc.appendChild(el("div", "empty",
        "开始提问吧。回答会附带<strong>页码引用</strong>，点击角标可查看来源。<br><br>" +
        "材料里没有的内容，知伴会明确告知「材料中未提及」，不编造。"));
    }
    S.messages.forEach((m) => sc.appendChild(msgNode(m.role, m.content, m.citations, m.content_json)));
    sc.scrollTop = sc.scrollHeight;
  }

  function msgNode(role, content, citations, guided) {
    const wrap = el("div", "msg " + (role === "user" ? "user" : "assistant"));
    wrap.appendChild(el("div", "who", role === "user" ? "我" : "知伴"));
    const bubble = el("div", "bubble md");
    if (role === "user") bubble.textContent = content;
    else MD.mount(bubble, content || "");
    wrap.appendChild(bubble);

    if (guided && role === "assistant") {
      const steps = guided.decomposition_steps || [];
      const qs = guided.follow_up_questions || [];
      // 这块结构化数据是**护栏的判据**（首轮不许给答案、必须 ≥2 步 ≥1 问），
      // 不是给学生看的内容。拆解步骤模型在正文里通常已经讲过一遍，重复贴出来
      // 只是噪音；mode/source 更是内部状态。所以：
      //   明面只留「学生需要行动的东西」= 本轮要回答的问题；
      //   拆解步骤与教学状态收进折叠区，想看时点开。
      if (qs.length) {
        const ask = el("div", "guided-ask");
        ask.appendChild(el("div", "ask-head", "🎯 请先回答"));
        qs.forEach((q) => ask.appendChild(el("div", "ask-q", escapeHtml(q))));
        wrap.appendChild(ask);
      }
      if (steps.length || guided.mode) {
        const det = el("details", "guided-more");
        det.innerHTML = "<summary>引导详情（拆解步骤 · 教学状态）</summary>";
        const inner = el("div");
        steps.forEach((s) => inner.appendChild(el("div", "hint",
          `${s.step}. <b>${escapeHtml(s.title)}</b>${s.hint ? " — " + escapeHtml(s.hint) : ""}`)));
        const meta = [guided.mode && `状态 ${guided.mode}`, guided.source && `来源 ${guided.source}`]
          .filter(Boolean).join(" · ");
        if (meta) inner.appendChild(el("div", "hint", meta));
        det.appendChild(inner);
        wrap.appendChild(det);
      }
    }

    (citations || []).forEach((c) => wrap.appendChild(citeCard(c)));
    return wrap;
  }

  /** 引用卡片：只显示短摘要 + 出处，点开才看原文（避免整段正文糊在对话里）。 */
  function citeCard(c) {
    const snip = String(c.snippet || "").replace(/\s+/g, " ").trim();
    const short = snip.length > 70 ? snip.slice(0, 70) + "…" : snip;
    const card = el("div", "cite-card",
      `<div class="n">[${c.n}] ${c.page_no != null ? "第 " + c.page_no + " 页" : ""}` +
      `<span style="float:right;color:var(--muted);font-weight:400">点击查看原文</span></div>` +
      `<div class="src">《${escapeHtml(c.document_title)}》${c.section ? " · " + escapeHtml(c.section) : ""}</div>` +
      `<div class="snip">${escapeHtml(short)}</div>`);
    card.title = snip;
    card.onclick = () => openPreview(c);
    return card;
  }

  function renderRight() {
    const box = document.getElementById("right-cites");
    box.innerHTML = "";
    box.appendChild(el("div", "panel-head", `<span>引用来源（${S.citations.length}）</span>`));
    const body = el("div", "panel-body");
    if (!S.citations.length) body.appendChild(el("div", "empty", "回答中的引用会显示在这里"));
    S.citations.forEach((c) => body.appendChild(citeCard(c)));
    box.appendChild(body);

    const mbox = document.getElementById("right-memory");
    mbox.innerHTML = "";
    mbox.appendChild(el("div", "panel-head",
      `<span>记忆（${S.memories.length}）</span><button class="btn small" data-nav="#/memory">管理</button>`));
    const mbody = el("div", "panel-body");
    S.memories.forEach((m) => {
      mbody.appendChild(el("div", "hint",
        `<span class="pill ${m.type === "knowledge_gap" ? "warn" : ""}">${typeName(m.type)}</span> ${escapeHtml(m.content.slice(0, 60))}`));
    });
    if (!S.memories.length) mbody.appendChild(el("div", "empty", "还没有记忆"));
    mbox.appendChild(mbody);
    mbox.querySelector("[data-nav]").onclick = () => location.hash = "#/memory";
  }

  function typeName(t) {
    return { preference: "偏好", progress: "进度", knowledge_gap: "盲区", fact: "事实" }[t] || t;
  }
  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ── 上传 ─────────────────────────────── */
  function triggerUpload() {
    const inp = el("input");
    inp.type = "file";
    inp.accept = ".pdf,.docx,.pptx,.md,.txt,.html,.htm";
    inp.multiple = true;
    inp.onchange = async () => {
      const fd = new FormData();
      [...inp.files].forEach((f) => fd.append("files", f));
      try {
        const d = await Api.upload("/api/documents/upload", fd);
        Toast(`已上传 ${d.documents.length} 个文件，解析中…`);
        await loadDocuments(); renderSide();
        d.documents.forEach(pollDoc);
      } catch (e) { Toast(e.message, true); }
    };
    inp.click();
  }

  async function pollDoc(doc) {
    for (let i = 0; i < 120; i++) {
      const d = await Api.get("/api/documents/" + doc.id);
      if (d.document.status === "ready") {
        if (d.document.warning) Toast(d.document.warning, true);
        await loadDocuments(); renderSide(); return;
      }
      if (d.document.status === "failed") { Toast("解析失败：" + d.document.error, true); return; }
      await new Promise((r) => setTimeout(r, 500));
    }
  }

  async function newConversation() {
    const c = await Api.post("/api/conversations", {
      mode: S.guided ? "guided" : "normal",
      document_ids: S.selectedDocs.length ? S.selectedDocs : null,
    });
    await loadConversations();
    await openConversation(c.id);
  }

  /* ── 发送 ─────────────────────────────── */
  async function send() {
    if (S.streaming) { if (S.abort) S.abort.abort(); return; }
    const ta = document.getElementById("chat-text");
    const text = ta.value.trim();
    if (!text) return;
    const badge = document.getElementById("model-badge");
    if (!badge.dataset.ok) {
      Toast(badge.dataset.missing === "model"
        ? "还差一项：接口地址已填，但没填「模型名」→ 点右上角或去「设置」补上"
        : "请先在「设置」里配置对话模型（接口地址 + 模型名）", true);
      return;
    }

    if (!S.conversationId) await newConversation();
    ta.value = "";
    S.streaming = true;
    setSendLabel("停止");
    S.citations = [];

    const sc = document.getElementById("chat-scroll");
    sc.appendChild(msgNode("user", text, [], null));
    const live = el("div", "msg assistant");
    live.appendChild(el("div", "who", "知伴"));
    const bubble = el("div", "bubble md");
    bubble.innerHTML = '<span class="hint">思考中…</span>';
    live.appendChild(bubble);
    sc.appendChild(live);
    sc.scrollTop = sc.scrollHeight;

    let acc = "";
    const paint = (t) => { MD.mount(bubble, t); sc.scrollTop = sc.scrollHeight; };

    S.abort = Api.stream("/api/chat/stream", {
      conversation_id: S.conversationId, message: text,
      guided: S.guided, document_ids: S.selectedDocs.length ? S.selectedDocs : null,
      grounding: S.grounding,
    }, {
      meta: (d) => { if (d.degraded) Toast("云端嵌入不可用，已用本地检索兜底"); },
      delta: (d) => { acc += d.text; paint(acc); },
      guided: (d) => {
        // 引导式结构化路径：直接展示服务端回填后的内容
        acc = d.summary ? MD.normalize(d.summary) : acc;
      },
      citation: (d) => {
        S.citations = d.citations || [];
        if (d.content) { acc = d.content; paint(acc); }
        renderRight();
        // 把引用卡片补进气泡
        (S.citations || []).forEach((c) => live.appendChild(citeCard(c)));
      },
      done: async () => {
        paint(acc);
        S.streaming = false; setSendLabel("发送");
        // 朗读：**每条新回答到达时**都要读（此前只在勾选开关那一下调用过一次，
        // 所以用户开了朗读却听不到后续回答）。
        if (S.ttsOn) speak(acc);
        await refreshAfterTurn();
      },
      error: (d) => {
        paint((acc || "") + `\n\n> ⚠️ ${d.message || "调用失败"}`);
        S.streaming = false; setSendLabel("发送");
        Toast(d.message || "调用失败", true);
      },
      close: () => { S.streaming = false; setSendLabel("发送"); },
    });
  }

  /* ── 引用定位（站内浮层预览原文，不再弹新窗口）───── */
  async function openPreview(c) {
    showOverlay(
      `《${c.document_title}》${c.page_no != null ? " · 第 " + c.page_no + " 页" : ""}`,
      c.section || "",
      async () => {
        try {
          const q = c.page_no != null ? "?page_no=" + c.page_no : "";
          const d = await Api.get(`/api/documents/${c.document_id}/preview${q}`);
          const pages = d.pages || [];
          return pages.map((p) => `【第 ${p.page_no} 页】\n${p.text}`).join("\n\n") || c.snippet || "";
        } catch (e) { return c.snippet || "（无法加载原文）"; }
      }
    );
  }

  /** 站内浮层：标题 + 可滚动正文（替代 window.open，避免弹出一堆窗口）。 */
  function showOverlay(title, sub, loadText) {
    const old = document.getElementById("zb-overlay");
    if (old) old.remove();
    const back = el("div");
    back.id = "zb-overlay";
    back.style.cssText = "position:fixed;inset:0;background:rgba(15,20,30,.45);z-index:9999;"
      + "display:flex;align-items:center;justify-content:center;padding:28px";
    const card = el("div", "card");
    card.style.cssText = "max-width:880px;width:100%;max-height:86vh;display:flex;flex-direction:column;background:var(--panel)";
    card.innerHTML =
      `<div class="panel-head" style="border-bottom:1px solid var(--border);padding-bottom:8px">
         <span>${escapeHtml(title)}</span>
         <button class="btn small" id="zb-ov-x">关闭</button>
       </div>
       <pre id="zb-ov-body" style="white-space:pre-wrap;overflow:auto;margin:12px 0 0;font-size:13px;flex:1">加载中…</pre>`;
    back.appendChild(card);
    back.onclick = (e) => { if (e.target === back) back.remove(); };
    document.body.appendChild(back);
    document.getElementById("zb-ov-x").onclick = () => back.remove();
    Promise.resolve()
      .then(loadText)
      .then((text) => {
        const b = document.getElementById("zb-ov-body");
        if (b) b.textContent = (sub ? `【${sub}】\n\n` : "") + text;
      })
      .catch((e) => {
        const b = document.getElementById("zb-ov-body");
        if (b) b.textContent = "加载失败：" + e.message;
      });
  }

  async function refreshAfterTurn() {
    await loadConversations();
    await loadMemories();
    renderSide(); renderRight();
    // 重渲染整个会话（拿到服务端落库后的最终消息与引用）
    if (S.conversationId) {
      const conv = await Api.get("/api/conversations/" + S.conversationId);
      S.messages = conv.messages || [];
      renderChat();
    }
  }

  function setSendLabel(t) {
    const b = document.getElementById("btn-send");
    b.textContent = t;
    b.classList.toggle("danger", t === "停止");
  }

  /* ── 语音输入（本地 ASR）─────────────────
   * 录音 / 转写 / 填框的通用实现已抽到 js/asr.js（课堂页也要用），
   * 这里只负责把按钮与目标输入框接上去，避免两处各写一份。 */
  function toggleRecord() {
    const btn = document.getElementById("btn-mic");
    if (!window.Asr) return Toast("语音模块未加载，请刷新页面", true);
    return Asr.toggle(btn, () => document.getElementById("chat-text"), {
      idleText: "🎙 按住说话",
      toast: (m, bad) => Toast(m, bad),
    });
  }

  /* ── 本地朗读（浏览器系统语音）────────── */
  // 具体实现已抽到公共模块 js/voice.js（课堂页也要用），这里只保留开关与云端分支。
  Voice.ensureVoices();
  // 引擎（系统语音 / 本地 MeloTTS）由后端设置决定，进入页面时同步一次。
  Voice.syncFromServer();

  function speak(text, force) {
    if (!S.tts || !S.tts.enabled || S.tts.mode === "off") return;
    if (!S.ttsOn && !force) return;
    if (S.tts.mode === "cloud") {
      Api.post("/api/tts/speech", { text: String(text).slice(0, 4000) })
        .then(() => Toast("云端音频已生成；云端朗读需要能直接播放的音频端点"));
      return;
    }
    Voice.speak(text, {
      onWarn: (m) => Toast(m, true),
      onError: (m) => Toast(m + "（可在「设置 → 语音」点「试听」排查，或改用云端朗读）", true),
    });
  }

  /* ── 页面装配 ─────────────────────────── */
  async function render(host) {
    await Promise.all([loadDocuments(), loadConversations(), loadMemories(), loadTTS()]);
    host.innerHTML = "";
    const cols = el("div", "cols");
    cols.innerHTML = `
      <div class="col col-side" style="flex-direction:column">
        <div id="side-docs" style="flex:1 1 55%;display:flex;flex-direction:column;min-height:0"></div>
        <div id="side-convs" style="flex:1 1 45%;display:flex;flex-direction:column;min-height:0;border-top:1px solid var(--border)"></div>
      </div>
      <div class="col col-main">
        <div class="chat-scroll" id="chat-scroll"></div>
        <div class="chat-input">
          <div class="box">
            <button class="btn" id="btn-mic" title="本地语音识别（录音 → 文字）">🎙 按住说话</button>
            <textarea id="chat-text" placeholder="输入问题，Enter 发送；Shift+Enter 换行"></textarea>
            <button class="btn primary" id="btn-send">发送</button>
          </div>
          <div class="chat-opts">
            <label class="switch"><input type="checkbox" id="opt-guided"> 🎓 引导式学习（先追问，不直接给答案）</label>
            <label class="switch"><input type="checkbox" id="opt-loose"> 允许材料外回答</label>
            <span style="flex:1"></span>
            <span id="opt-tts-wrap"></span>
            <span id="opt-scope">范围：全库</span>
          </div>
        </div>
      </div>
      <div class="col col-right" id="right-cites"></div>
      <div class="col col-right" id="right-memory"></div>`;
    host.appendChild(cols);

    renderSide(); renderChat(); renderRight();

    const ta = document.getElementById("chat-text");
    ta.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
    document.getElementById("btn-send").onclick = send;
    document.getElementById("btn-mic").onclick = toggleRecord;
    document.getElementById("opt-guided").checked = S.guided;
    document.getElementById("opt-guided").onchange = (e) => { S.guided = e.target.checked; };
    document.getElementById("opt-loose").onchange = (e) => { S.grounding = e.target.checked ? "loose" : "strict"; };
    updateScope();

    // 朗读开关（状态记在 S.ttsOn，切换页面后仍然保持）
    const tw = document.getElementById("opt-tts-wrap");
    if (S.tts && S.tts.mode === "local") {
      const lab = el("label", "switch", `<input type="checkbox" id="opt-tts"> 🔊 朗读`);
      tw.appendChild(lab);
      const cb = document.getElementById("opt-tts");
      cb.checked = S.ttsOn;
      cb.onchange = (e) => {
        S.ttsOn = e.target.checked;
        if (S.ttsOn) {
          const last = [...S.messages].reverse().find((m) => m.role === "assistant");
          speak(last ? last.content : "朗读已开启，之后的回答我会读出来。", true);
        } else {
          speechSynthesis.cancel();
        }
      };
    } else if (S.tts && S.tts.mode === "cloud") {
      const lab = el("label", "switch", `<input type="checkbox" id="opt-tts"> 🔊 朗读（云端）`);
      tw.appendChild(lab);
      const cb = document.getElementById("opt-tts");
      cb.checked = S.ttsOn;
      cb.onchange = (e) => { S.ttsOn = e.target.checked; };
    } else {
      tw.innerHTML = `<span class="pill">朗读未启用</span>`;
      tw.title = "在「设置 → 语音」中开启";
    }

    // 若上次会话存在则恢复
    if (S.conversationId) renderChat();
    else if (S.conversations.length) await openConversation(S.conversations[0].id);
  }

  function updateScope() {
    const n = S.selectedDocs.length;
    document.getElementById("opt-scope").textContent = n ? `范围：已选 ${n} 份` : "范围：全库";
  }

  // 选中材料变化时同步提示
  const origRenderSide = renderSide;
  renderSide = function () { origRenderSide(); updateScope(); };

  window.Views = window.Views || {};
  window.Views.workbench = { render };
})();
