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
    // 右栏三块面板的折叠状态（引用 / 记忆 / 文档预览）。收起后只留一条竖条，
    // 把横向空间让给聊天区 —— 三块全展开会把聊天区挤得很窄。
    fold: { cites: false, memory: false, preview: false },
    previewDoc: null,      // 右栏正在预览的文档：{ id, title, doc }（渲染交给 DocPreview）
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
    // 重建前记下列表滚动位置，建完还原：上传 / 重新解析会整体重建列表，
    // 不还原的话用户滚到一半就被弹回顶部（与「选中跳顶」同一类问题）。
    const prevScroll = (docs.querySelector(".panel-body") || {}).scrollTop || 0;
    docs.innerHTML = "";
    const head = el("div", "panel-head",
      `<span>资料库（${S.documents.length}）</span><button class="btn small primary" id="btn-upload">＋ 上传</button>`);
    docs.appendChild(head);
    // 用户反馈：不知道要先选文件再提问。这行放在「＋ 上传」正下方，最先被看到。
    docs.appendChild(el("div", "hint",
      "点击选择参考文件以进行提问（可多选；不选 = 使用全库）"));
    const body = el("div", "panel-body");
    if (!S.documents.length) {
      body.appendChild(el("div", "empty",
        "还没有资料。<br>点上方「＋ 上传」，或把 PDF / DOCX / PPTX / MD / TXT / HTML <b>直接拖到这里</b>。"));
    }
    S.documents.forEach((d) => {
      const item = el("div", "item" + (S.selectedDocs.includes(d.id) ? " active" : ""));
      const icon = { pdf: "📕", docx: "📘", pptx: "📙", md: "📗", txt: "📒", html: "🌐" }[d.fmt] || "📄";
      item.appendChild(el("span", null, icon));
      item.appendChild(el("span", "t", `${escapeHtml(d.title)}`));
      // 选中要有明确回执：一个对勾 + 左侧主色竖条（用户反馈「只是加深一点，提示不明显」）
      if (S.selectedDocs.includes(d.id)) item.appendChild(el("span", "picked", "✓"));
      item.appendChild(el("small", null, d.status === "ready" ? `${d.page_count || ""}` : d.status));
      const pv = el("button", "pv", "👁");
      pv.title = "在右侧预览原文";
      pv.onclick = (ev) => { ev.stopPropagation(); openDocPreview(d); };
      item.appendChild(pv);
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
        // ⚠️ 就地更新这一项的选中态，**不要整列表重建**：重建会把滚动容器清空，
        // 列表长了以后每选一份就被弹回顶部，得反复下滑（用户实测反馈）。
        const on = S.selectedDocs.includes(d.id);
        item.classList.toggle("active", on);
        const mark = item.querySelector(".picked");
        if (on && !mark) {
          const m = el("span", "picked", "✓");
          item.insertBefore(m, item.querySelector("small"));
        } else if (!on && mark) {
          mark.remove();
        }
        updateScope();      // 底部「范围：已选 N 份」随份数变化
      };
      item.title = d.warning || d.title;
      body.appendChild(item);
    });
    docs.appendChild(body);
    if (prevScroll) body.scrollTop = prevScroll;

    const convs = document.getElementById("side-convs");
    convs.innerHTML = "";
    convs.appendChild(el("div", "panel-head",
      `<span>会话</span><button class="btn small" id="btn-newconv">＋ 新建</button>`));
    const cbody = el("div", "panel-body");
    S.conversations.forEach((c) => {
      const item = el("div", "item" + (c.id === S.conversationId ? " active" : ""));
      item.appendChild(el("span", "t", `${c.mode === "guided" ? "🎓 " : ""}${escapeHtml(c.title || "新的会话")}`));
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

    // 拖拽上传：把文件拖到「资料库」面板即可（与点「＋ 上传」共用 uploadFiles）。
    // 用 onXxx **赋值**（不是 addEventListener）—— renderSide 会重画多次，赋值天然防重复绑定。
    const drop = document.getElementById("side-docs");
    if (drop) {
      drop.ondragover = (ev) => {
        ev.preventDefault();
        const types = ev.dataTransfer && ev.dataTransfer.types;
        if (types && Array.prototype.indexOf.call(types, "Files") >= 0) {
          drop.classList.add("dropping");
        }
      };
      drop.ondragleave = () => drop.classList.remove("dropping");
      drop.ondrop = (ev) => {
        ev.preventDefault();
        drop.classList.remove("dropping");
        const fl = ev.dataTransfer && ev.dataTransfer.files;
        if (fl && fl.length) uploadFiles(fl);
        else Toast("请拖入文件（暂不支持文件夹）", true);
      };
    }
  }

  function renderChat() {
    const sc = document.getElementById("chat-scroll");
    if (!sc) return;
    sc.innerHTML = "";
    if (!S.messages.length) {
      sc.appendChild(el("div", "empty",
        "<b>三步开始：</b><br>" +
        "① 先在左边「资料库」勾选要参考的文件（可多选；不选 = 使用全库）<br>" +
        "② 再按实际情况决定是否勾选下方的「允许材料外回答」——" +
        "不勾就只依据材料，材料里没有的会明确告知<br>" +
        "③ 最后在下面的方框里提问<br><br>" +
        "回答会附带<strong>页码引用</strong>，点击角标可查看来源。"));
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

  /** 右栏面板标题栏：整条可点，用来折叠 / 展开该面板。
   *
   *  @param {string} title 展开态标题（可带计数）
   *  @param {string} key   S.fold 里的键
   *  @param {string} short 收起态显示的短标签（"引用来源" → "引用"）——
   *                        收起后只有 44px 宽，全称竖排会拖得很长
   *  @param {string} [extra] 展开态才显示的按钮 HTML
   *
   *  ⚠️ 收起态**不渲染** extra：早先想用 CSS 隐藏它，但这里写的是内联
   *  `display:flex`，内联优先级压过样式表里的 `display:none` —— 结果「管理」按钮
   *  在收起态被竖排露出来（用户截图反馈）。按钮要么不渲染，要么别用内联样式。
   */
  function foldHead(title, key, short, extra) {
    const folded = S.fold[key];
    const h = el("div", "panel-head clickable");
    if (folded) {
      h.innerHTML = `<span class="fold-label">${short}</span>` +
        `<span class="fold" title="展开">▸</span>`;
    } else {
      h.innerHTML = `<span>${title}</span>` +
        `<span class="ph-right">${extra || ""}<span class="fold" title="收起">◂</span></span>`;
    }
    h.onclick = (e) => {
      if (e.target.closest("button")) return;   // 「管理」「关闭」这类按钮不触发折叠
      S.fold[key] = !S.fold[key];
      renderRight();
    };
    return h;
  }

  function renderRight() {
    // ── 引用来源 ──
    const box = document.getElementById("right-cites");
    if (box) {
      box.className = "col col-right" + (S.fold.cites ? " folded" : "");
      box.innerHTML = "";
      box.appendChild(foldHead(`引用来源（${S.citations.length}）`, "cites", "引用"));
      const body = el("div", "panel-body");
      if (!S.citations.length) body.appendChild(el("div", "empty", "回答中的引用会显示在这里"));
      S.citations.forEach((c) => body.appendChild(citeCard(c)));
      box.appendChild(body);
    }

    // ── 记忆 ──
    const mbox = document.getElementById("right-memory");
    if (mbox) {
      mbox.className = "col col-right" + (S.fold.memory ? " folded" : "");
      mbox.innerHTML = "";
      mbox.appendChild(foldHead(`记忆（${S.memories.length}）`, "memory", "记忆",
        `<button class="btn small" data-nav="#/memory">管理</button>`));
      const mbody = el("div", "panel-body");
      S.memories.forEach((m) => {
        mbody.appendChild(el("div", "hint",
          `<span class="pill ${m.type === "knowledge_gap" ? "warn" : ""}">${typeName(m.type)}</span> ${escapeHtml(m.content.slice(0, 60))}`));
      });
      if (!S.memories.length) mbody.appendChild(el("div", "empty", "还没有记忆"));
      mbox.appendChild(mbody);
      // ⚠️ 收起态 foldHead 不渲染「管理」按钮（见 foldHead 注释），这里必须判空 ——
      // 否则点 👁 预览时（会顺手把记忆面板折叠）renderRight 在这行抛
      // TypeError，**后面的预览面板渲染整段跳过**（2026-09-18 用户实测踩坑）。
      const navBtn = mbox.querySelector("[data-nav]");
      if (navBtn) navBtn.onclick = () => location.hash = "#/memory";
    }

    // ── 文档预览（点资料库里的 👁 才出现；没预览时整块不占地方）──
    const pbox = document.getElementById("right-preview");
    if (pbox) {
      const d = S.previewDoc;
      pbox.style.display = d ? "" : "none";
      if (d) {
        const folded = !!S.fold.preview;
        pbox.className = "col col-right col-preview" + (folded ? " folded" : "");
        // 展开时用「记住的宽度」（默认 440，见 CSS 注释）；收起时交回 CSS 的 28px
        pbox.style.width = folded ? "" : previewWidth() + "px";
        pbox.innerHTML = "";
        // 拖拽把手：贴在面板左边缘（绝对定位，不占列宽）
        const rz = el("div", "pv-resizer");
        rz.title = "拖动调整预览宽度；双击复位";
        rz.onmousedown = startPreviewDrag;
        rz.ondblclick = () => { setPreviewWidth(PREVIEW_W_DEFAULT); renderRight(); };
        pbox.appendChild(rz);
        pbox.appendChild(foldHead(`文档预览《${escapeHtml(d.title)}》`, "preview", "预览",
          `<button class="btn small" id="pv-full" title="全屏预览（PDF 原页放大看）">⤢</button>`
          + `<button class="btn small" id="pv-close">关闭</button>`));
        const pb = el("div", "panel-body");
        pbox.appendChild(pb);
        const cb = pbox.querySelector("#pv-close");
        if (cb) cb.onclick = closeDocPreview;
        const fb = pbox.querySelector("#pv-full");
        if (fb) fb.onclick = () => openFullPreview(d.doc);
        // 渲染交给 DocPreview：PDF 用 pdf.js 画原页图像，其他格式按页取文本。
        // 别再直接调 /api/documents/{id}/preview —— 那个端点只返回一页（引用定位用）。
        if (d.doc && window.DocPreview) DocPreview.mount(pb, d.doc, { startPage: d.page || 1 });
      }
    }
  }

  function typeName(t) {
    return { preference: "偏好", progress: "进度", knowledge_gap: "盲区", fact: "事实" }[t] || t;
  }
  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ── 上传 ─────────────────────────────── */
  /** 真正上传：点「＋ 上传」选文件与**拖拽落文件**共用这一条路径。 */
  async function uploadFiles(files) {
    const list = [...(files || [])].filter((f) => f && f.name);
    if (!list.length) return;
    const fd = new FormData();
    list.forEach((f) => fd.append("files", f));
    try {
      const d = await Api.upload("/api/documents/upload", fd);
      Toast(`已上传 ${d.documents.length} 个文件，解析中…`);
      await loadDocuments(); renderSide();
      d.documents.forEach(pollDoc);
    } catch (e) { Toast(e.message, true); }
  }

  function triggerUpload() {
    const inp = el("input");
    inp.type = "file";
    inp.accept = ".pdf,.docx,.pptx,.md,.txt,.html,.htm";
    inp.multiple = true;
    inp.onchange = () => uploadFiles(inp.files);
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

  /* ── 预览面板宽度（可拖拽，记住选择）────────── */
// 为什么可调：窗口大小、材料类型（单栏 PDF / 双栏 PDF / md 文本）差异很大，
// 一个固定宽度必然有人嫌窄 —— 用户实测「预览窗口太小、PDF 看不清」。
const PREVIEW_W_DEFAULT = 440;
const PREVIEW_W_KEY = "zhiban-preview-w";
let previewW = null;                    // 懒读 localStorage

function previewWidth() {
  if (previewW == null) {
    const v = parseInt(localStorage.getItem(PREVIEW_W_KEY) || "", 10);
    previewW = Number.isFinite(v) && v >= 260 ? v : PREVIEW_W_DEFAULT;
  }
  return previewW;
}
function setPreviewWidth(w) {
  const max = Math.max(320, window.innerWidth - 400);   // 给左侧聊天区留出可读宽度
  previewW = Math.max(260, Math.min(max, Math.round(w)));
  try { localStorage.setItem(PREVIEW_W_KEY, String(previewW)); } catch (e) { /* 隐私模式等，忽略 */ }
}
/** 拖预览面板左边缘改宽度：面板在最右侧，鼠标往左移 = 变宽。 */
function startPreviewDrag(ev) {
  ev.preventDefault();
  const startX = ev.clientX;
  const startW = previewWidth();
  const bar = ev.currentTarget;
  bar.classList.add("dragging");
  document.body.style.cursor = "col-resize";
  const move = (e) => {
    const box = document.getElementById("right-preview");
    if (!box) return;
    setPreviewWidth(startW + (startX - e.clientX));
    box.style.width = previewWidth() + "px";
  };
  const up = () => {
    bar.classList.remove("dragging");
    document.body.style.cursor = "";
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
}
/** 全屏预览：小面板里 PDF 原页再宽也就 400 多像素，看不清时一次性放大到近全屏。
 *  ESC / 点空白 / 「关闭」都可退出。 */
function openFullPreview(doc) {
  if (!doc || !window.DocPreview) return;
  const old = document.getElementById("pv-full-box");
  if (old) old.remove();
  const back = el("div", "pv-full");
  back.id = "pv-full-box";
  const card = el("div", "pv-full-card");
  card.innerHTML = `<div class="panel-head"><span>《${escapeHtml(doc.title || "材料")}》</span>`
    + `<button class="btn small" id="pv-full-x">关闭（Esc）</button></div>`;
  const body = el("div", "panel-body");
  card.appendChild(body);
  back.appendChild(card);
  const onKey = (e) => { if (e.key === "Escape") close(); };
  function close() {
    document.removeEventListener("keydown", onKey);
    back.remove();
  }
  back.onclick = (e) => { if (e.target === back) close(); };
  document.body.appendChild(back);
  const x = document.getElementById("pv-full-x");
  if (x) x.onclick = close;
  document.addEventListener("keydown", onKey);
  DocPreview.mount(body, doc, { startPage: 1 });
}

/* ── 右栏文档预览（点资料库里的 👁）──────────────────
   和「点引用角标看原文」是两条路：那个只弹**引用命中的那一页**（浮层，看完就关）；
   这个把整份文档摊在右栏、边看边提问。
   打开时自动把「引用来源」「记忆」收起来 —— 否则三块全展开会把聊天区挤没。 */
function openDocPreview(d, page) {
  // 渲染与取数都在 DocPreview 组件里（PDF 原页 / 其他格式文本），这里只记状态
  S.previewDoc = { id: d.id, title: d.title, doc: d, page: page || 1 };
  S.fold.cites = true; S.fold.memory = true;   // 给预览腾地方
  // ⚠️ 点 👁 = 明确要「看」。若预览面板曾被手动收起（点标题栏会折叠它，且收起态
  // 没有「关闭」按钮、标题也看不见），再点 👁 必须强制展开并换上新文档 ——
  // 否则用户看到的就是「点了没反应 / 换文件不切换」（2026-09-18 用户实测反馈）。
  S.fold.preview = false;
  renderRight();
}

function closeDocPreview() {
  S.previewDoc = null;
  S.fold.cites = false; S.fold.memory = false;   // 收起的原因没了，恢复展开
  renderRight();
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
    // 引擎（system/melo/cloud）由设置决定；失败原样显示原因，绝不套「改用云端朗读」。
    Voice.speak(text, {
      onWarn: (m) => Toast(m, true),
      onError: (m) => Toast(m, true),
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
      <div class="col col-right" id="right-memory"></div>
      <div class="col col-right" id="right-preview" style="display:none"></div>`;
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
    if (S.tts && S.tts.enabled && S.tts.mode !== "off") {
      const isCloud = S.tts.mode === "cloud";
      const label = isCloud ? "🔊 朗读（云端）" : "🔊 朗读";
      const lab = el("label", "switch", `<input type="checkbox" id="opt-tts"> ${label}`);
      tw.appendChild(lab);
      const cb = document.getElementById("opt-tts");
      cb.checked = S.ttsOn;
      cb.onchange = (e) => {
        S.ttsOn = e.target.checked;
        if (S.ttsOn) {
          const last = [...S.messages].reverse().find((m) => m.role === "assistant");
          speak(last ? last.content : "朗读已开启，之后的回答我会读出来。", true);
        } else {
          Voice.stop();   // cloud/melo/system 都要能停
        }
      };
      // 云端模式：未配置时追加「未配置」徽标，点它去设置页排查。
      if (isCloud && S.tts.cloud_configured === false) {
        const pill = document.createElement("span");
        pill.className = "pill bad";
        pill.textContent = "云端未配置";
        pill.style.marginLeft = "8px";
        pill.title = "去设置 → 语音 填写端点与模型名，可点「测试连接」确认";
        tw.appendChild(pill);
      }
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
