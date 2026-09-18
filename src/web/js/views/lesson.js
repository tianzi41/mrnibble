/* 讲次课堂：讲义白板 / 材料标注 / 单元总结 + 引用 + 课堂提问 + 导出 + 朗读。 */
(function () {
  "use strict";

  const S = {
    lesson: null,
    course: null,
    unit: null,
    conversationId: null,
    messages: [],
    streaming: false,
    abort: null,
    questionCount: 0,
    tab: "board",           // board | marks | summary
    // 材料标注
    marks: [],
    docIndex: 0,            // 当前查看的材料下标
    pageNo: 1,
    pageCount: 1,
    scale: 1.2,
    mode: "view",           // view | highlight | circle
    drag: null,
    saveTimer: null,
    // 左侧「课本原件」栏（与上面「材料标注」tab 各自独立：那个是编辑标注用的，
    // 这个只是把原件摊在旁边边看边听）
    originDoc: null,        // 当前显示的材料 id；null = 自动选本讲引用最多的那份
    originPage: 1,
    originFolded: false,    // 收起后变成一条竖标签，把宽度让给讲义
    originHandle: null,     // DocPreview 返回的跳页句柄
    docMeta: null,          // id -> { fmt, page_count, title }；null = 还没拉
    // 单元总结
    unitSummary: null,
    // 上课流
    readyShown: false,
    enteredWithTab: false,
    // 右侧面板
    citesOpen: false,       // 引用来源：默认收起
    speaking: "",           // 讲师当前正在讲述的内容（实时）
    subtitle: "",           // 屏幕中下方字幕（与语音同步，逐句更新）
    // 课件 / 讲授进度
    slides: [],             // 学生看的课件页（优先来自 lesson.slides）
    scripts: [],            // 讲师讲稿（按 slide_id 关联课件）
    slideIndex: 0,          // 当前讲到第几页（0-based）
    teaching: false,        // 是否正在上课讲授中
    speakLog: [],           // 讲师讲述历史（已讲完的每页一段，可上翻）
    paused: false,          // （已废弃）暂停式互动检查点已按用户要求永久移除；字段保留仅为兼容
    finished: false,        // 本讲已讲完（显示下一步选项）
    noPause: false,         // （已废弃）同上
    voicePaused: false,     // 用户主动暂停朗读
    lessonId: null,         // 当前课堂对应的讲次 id（用于切页续讲）
  };

  /** 卡片「内容类型」→ [显示名, 底色]。
   *  底色**必须走 CSS 变量**，不能写死色值 —— 写死的话主题（暗色）管不到它，
   *  卡片保持浅底、而文字跟着主题变浅 → 「浅底浅字」直接看不清（2026-09-17 实测踩到）。 */
  const KIND = {
    concept: ["概念", "var(--card-concept)"], example: ["例子", "var(--card-example)"],
    formula: ["公式", "var(--card-formula)"], quote: ["材料原文", "var(--card-quote)"],
    note: ["补充", "var(--card-note)"],
    diagram: ["图示", "var(--card-diagram)"], chart: ["图表", "var(--card-chart)"],
    table: ["对比", "var(--card-table)"], takeaway: ["金句", "var(--card-takeaway)"],
  };

  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };
  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /** 行内 Markdown 渲染（要点列表用）。MD.inline 不可用时退回纯文本转义，
   *  保证任何加载顺序下都不会把未清洗的内容塞进 DOM。 */
  const mdInline = (s) =>
    (window.MD && typeof window.MD.inline === "function" ? window.MD.inline(s) : esc(s));

  // 字幕粒度：按句子切块朗读，每块最多这么多字。
  // 越小字幕越跟得紧，但语音停顿会变碎；90 字约等于 1~2 句，是实测较平衡的值。
  const SUB_CHARS = 90;

  /* ── 数据 ─────────────────────────────── */
  async function loadLesson(id) {
    S.lesson = await Api.get("/api/courses/lessons/" + id);
    S.course = await Api.get("/api/courses/" + S.lesson.course_id);
    S.unit = (S.course.units || []).find((u) => u.id === S.lesson.unit_id) || null;
    S.conversationId = S.lesson.conversation_id || null;
    S.marks = S.lesson.marks || [];
    S.questionCount = S.lesson.question_count || 0;
    const docs = courseDocuments();
    S.docIndex = Math.min(S.docIndex, Math.max(0, docs.length - 1));
  }

  /** 课程绑定材料（标题优先取标注里带的，其次用引用里的）。 */
  function courseDocuments() {
    const ids = (S.course && S.course.document_ids) || [];
    const meta = S.docMeta || {};
    return ids.map((id) => {
      const hit = S.marks.find((m) => m.document_id === id)
        || (S.lesson.citations || []).find((c) => c.document_id === id);
      const m = meta[id] || {};
      // fmt / page_count 来自文档列表（原件预览靠 fmt 决定走 PDF 原页还是文本）
      return {
        id,
        title: (hit && hit.document_title) || m.title || "材料",
        fmt: m.fmt || "",
        page_count: m.page_count || 0,
      };
    });
  }

  /** 拉一次文档元信息（fmt / page_count）备用；失败也要置空对象 ——
   *  否则 renderOrigin 每次渲染都会重新拉，形成循环。 */
  async function ensureDocMeta() {
    if (S.docMeta) return;
    try {
      const d = await Api.get("/api/documents?page=1&page_size=200");
      const m = {};
      (d.items || []).forEach((x) => {
        m[x.id] = { fmt: x.fmt, page_count: x.page_count, title: x.title };
      });
      S.docMeta = m;
    } catch (e) {
      S.docMeta = {};
    }
  }

  /** 左侧「课本原件」栏。 */
  function renderOrigin() {
    const box = document.getElementById("lesson-origin");
    if (!box) return;
    box.className = "col col-origin" + (S.originFolded ? " folded" : "");
    box.innerHTML = "";
    S.originHandle = null;

    // 收起态：一条竖标签，点一下展开
    if (S.originFolded) {
      const bar = el("div", "panel-head clickable");
      bar.innerHTML = '<span class="fold-label">课本</span><span class="fold" title="展开">▸</span>';
      bar.onclick = () => { S.originFolded = false; renderOrigin(); };
      box.appendChild(bar);
      return;
    }

    const docs = courseDocuments();
    // 选默认材料：用户显式选过就用它 → 否则本讲引用里出现的第一份 → 再否则第一份材料
    let doc = docs.find((x) => x.id === S.originDoc);
    if (!doc) {
      const cited = ((S.lesson && S.lesson.citations) || []).find(
        (c) => docs.some((x) => x.id === c.document_id));
      doc = cited ? docs.find((x) => x.id === cited.document_id) : docs[0];
      S.originDoc = doc ? doc.id : null;
      if (cited && cited.page_no) S.originPage = cited.page_no;
    }

    const head = el("div", "panel-head");
    head.appendChild(el("span", null, "课本原件"));
    const right = el("span", "ph-right");
    if (docs.length > 1) {
      const sel = el("select", "mini-sel");
      sel.title = "切换这门课的其他材料";
      docs.forEach((x) => {
        const o = document.createElement("option");
        o.value = x.id;
        o.textContent = x.title || "材料";
        if (doc && x.id === doc.id) o.selected = true;
        sel.appendChild(o);
      });
      sel.onchange = () => { S.originDoc = sel.value; S.originPage = 1; renderOrigin(); };
      right.appendChild(sel);
    }
    const fold = el("button", "btn small", "收起");
    fold.title = "收起原件栏，把宽度让给讲义";
    fold.onclick = () => { S.originFolded = true; renderOrigin(); };
    right.appendChild(fold);
    head.appendChild(right);
    box.appendChild(head);

    const body = el("div", "panel-body");
    box.appendChild(body);
    if (!doc) {
      body.appendChild(el("div", "empty", "这门课还没绑定材料。"));
      return;
    }
    if (!window.DocPreview) {
      body.appendChild(el("div", "empty", "预览组件未加载。"));
      return;
    }
    // 元信息还没到（首次）→ 先渲染，拿到后自动重建一次
    if (!S.docMeta) ensureDocMeta().then(() => { if (S.docMeta) renderOrigin(); });
    DocPreview.mount(body, {
      id: doc.id,
      title: doc.title,
      fmt: doc.fmt,
      page_count: doc.page_count,
    }, { startPage: S.originPage || 1 }).then((h) => { S.originHandle = h; });
  }

  /** 讲义里点引用角标 [N] → 左侧原件翻到那一页。
   *  引用的可能是另一份材料 → 先切材料再跳页。 */
  function jumpOriginToCite(n) {
    const c = ((S.lesson && S.lesson.citations) || []).find((x) => String(x.n) === String(n));
    if (!c) return;
    S.originPage = c.page_no || 1;
    if (c.document_id && c.document_id !== S.originDoc) {
      S.originDoc = c.document_id;
      renderOrigin();
      return;
    }
    if (S.originFolded) { S.originFolded = false; renderOrigin(); return; }
    if (S.originHandle && S.originHandle.goTo) S.originHandle.goTo(S.originPage);
  }

  async function ensureConversation() {
    if (S.conversationId) {
      try {
        const conv = await Api.get("/api/conversations/" + S.conversationId);
        S.messages = conv.messages || [];
        return;
      } catch (e) { S.conversationId = null; }
    }
    const c = await Api.post("/api/conversations", {
      title: S.lesson.title, mode: "normal",
      document_ids: (S.course && S.course.document_ids) || null,
    });
    S.conversationId = c.id;
    S.messages = [];
    await Api.put("/api/courses/lessons/" + S.lesson.id, { conversation_id: c.id });
  }

  /** 统一轮询生成任务（讲义 / 练习 / 单元总结共用）。 */
  async function pollJob(jobId, onStage) {
    for (let i = 0; i < 300; i++) {
      const job = await Api.get("/api/courses/jobs/" + jobId);
      if (onStage) onStage(job.stage);
      if (job.status === "ready") return job;
      if (job.status === "failed") throw new Error(job.error || "生成失败");
      await new Promise((r) => setTimeout(r, 700));
    }
    throw new Error("生成超时，请重试");
  }

  /* ── 页签 1：讲义白板 ─────────────────── */
  function renderBoard(host) {
    const board = S.lesson.board;
    const wrap = el("div", "board");
    if (!board) {
      // 空状态也必须挂到 host 上（曾经这里直接 return，导致整页空白）
      const empty = el("div", "empty");
      empty.innerHTML = "这一节还没有讲义。<br>"
        + "点下面的按钮，系统会依据你的材料生成本讲的白板内容。";
      const go = el("button", "btn primary", "生成讲义，开始学习");
      go.style.marginTop = "14px";
      go.onclick = () => startLecture(go);
      const holder = el("div");
      holder.appendChild(go);
      empty.appendChild(holder);
      wrap.appendChild(empty);
      host.appendChild(wrap);
      return;
    }
    wrap.appendChild(el("div", "hint",
      "这里是讲义全文（供你通读与复习）；上课时逐页展示的课件，请切到上面的「课件」页签。"));

    if (board.summary) wrap.appendChild(el("div", "board-summary", esc(board.summary)));

    const outline = board.outline || [];
    if (outline.length) {
      const box = el("div", "board-sec");
      box.appendChild(el("div", "sec-title", "本讲要点"));
      const ol = el("ol");
      outline.forEach((o) => ol.appendChild(el("li", null, esc(o))));
      box.appendChild(ol);
      wrap.appendChild(box);
    }

    const kps = board.keypoints || [];
    if (kps.length) {
      const box = el("div", "board-sec");
      box.appendChild(el("div", "sec-title", "关键术语"));
      const tbl = el("table", "tbl");
      tbl.innerHTML = "<thead><tr><th style='width:30%'>术语</th><th>说明</th></tr></thead>";
      const tb = el("tbody");
      kps.forEach((k) => {
        const tr = el("tr");
        tr.innerHTML = `<td><b>${esc(k.term)}</b></td><td>${esc(k.desc)}</td>`;
        tb.appendChild(tr);
      });
      tbl.appendChild(tb);
      box.appendChild(tbl);
      wrap.appendChild(box);
    }

    if (board.recap) {
      const box = el("div", "board-sec");
      box.appendChild(el("div", "sec-title", "本讲回顾"));
      const b = el("div", "md");
      MD.mount(b, board.recap);
      box.appendChild(b);
      wrap.appendChild(box);
    }

    // AI 标注的材料位置（点一下跳到「材料标注」页）
    const marks = board.marks || [];
    if (marks.length) {
      const box = el("div", "board-sec");
      box.appendChild(el("div", "sec-title", "材料标注"));
      box.appendChild(el("div", "hint", "点条目可跳到「材料标注」页查看对应位置。"));
      marks.forEach((m) => {
        const row = el("div", "mark-row");
        row.innerHTML = `<span class="pill ${m.kind === "circle" ? "warn" : ""}">`
          + `${m.kind === "circle" ? "圈注" : "高亮"}</span>`
          + `<span class="t">《${esc(m.document_title || "材料")}》`
          + `${m.page_no != null ? " 第 " + m.page_no + " 页" : ""}</span>`
          + `<span class="hint">${esc(m.text || "")}</span>`;
        row.onclick = () => {
          jumpToMark(m);
          S.tab = "marks";
          renderTabs(); renderTabBody();
        };
        box.appendChild(row);
      });
      wrap.appendChild(box);
    }
    host.appendChild(wrap);
  }

  /* ── 页签 2：材料标注 ─────────────────── */
  function jumpToMark(m) {
    const docs = courseDocuments();
    const i = docs.findIndex((d) => d.id === m.document_id);
    if (i >= 0) S.docIndex = i;
    S.pageNo = m.page_no || 1;
  }

  function renderMarks(host) {
    const docs = courseDocuments();
    if (!docs.length) {
      host.appendChild(el("div", "empty", "这门课没有绑定材料，无法标注。"));
      return;
    }
    if (!window.PdfView) {
      host.appendChild(el("div", "empty", "pdf.js 未加载，无法渲染材料原页。"));
      return;
    }
    if (!window.PdfView.available()) {
      // 组件是 ES module，可能还在加载：等它就绪后自动重画一次。
      host.appendChild(el("div", "empty", "正在加载材料渲染组件…"));
      window.PdfView.ready().then((ok) => {
        if (ok) { renderTabBody(); return; }
        host.innerHTML = '<div class="empty">pdf.js 加载失败，请刷新页面重试。'
          + (window.PdfJSError ? `<div class="hint">${esc(window.PdfJSError)}</div>` : "")
          + "</div>";
      });
      return;
    }
    const doc = docs[S.docIndex];

    const bar = el("div", "mk-bar");
    bar.innerHTML = `
      <button class="btn small" id="mk-prev">‹ 上一页</button>
      <span class="hint" id="mk-page">第 ${S.pageNo} 页</span>
      <button class="btn small" id="mk-next">下一页 ›</button>
      <span class="sep-v"></span>
      <button class="btn small" id="mk-zoom-out">－</button>
      <button class="btn small" id="mk-zoom-in">＋</button>
      <span class="sep-v"></span>
      <span class="hint">工具</span>
      <button class="btn small" data-mode="view">浏览</button>
      <button class="btn small" data-mode="highlight">高亮</button>
      <button class="btn small" data-mode="circle">圈注</button>
      <span class="sep-v"></span>
      <button class="btn small danger" id="mk-clear">清除本页</button>
      <span style="flex:1"></span>
      <span class="hint">${esc(doc.title)}</span>`;
    host.appendChild(bar);

    const layout = el("div", "mk-layout");
    const viewerWrap = el("div", "mk-viewer");
    const stage = el("div", "mk-stage");
    const canvas = document.createElement("canvas");
    const overlay = el("div", "mk-overlay");
    stage.appendChild(canvas);
    stage.appendChild(overlay);
    viewerWrap.appendChild(stage);
    layout.appendChild(viewerWrap);

    const side = el("div", "mk-notes");
    const head = el("div", "panel-head");
    head.innerHTML = "<span>旁注</span>";
    side.appendChild(head);
    const noteBody = el("div", "panel-body");
    side.appendChild(noteBody);
    layout.appendChild(side);

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "mk-links");
    layout.appendChild(svg);
    host.appendChild(layout);
    host.appendChild(el("div", "hint",
      "选「高亮」或「圈注」后在材料上拖出矩形即可标注；在右侧写旁注，连线会自动指向对应位置。标注会随课程保存。"));

    document.getElementById("mk-prev").onclick = () => {
      if (S.pageNo > 1) { S.pageNo--; renderTabBody(); }
    };
    document.getElementById("mk-next").onclick = () => {
      if (S.pageNo < S.pageCount) { S.pageNo++; renderTabBody(); }
    };
    document.getElementById("mk-zoom-in").onclick = () => {
      S.scale = Math.min(3, S.scale + 0.2); renderTabBody();
    };
    document.getElementById("mk-zoom-out").onclick = () => {
      S.scale = Math.max(0.6, S.scale - 0.2); renderTabBody();
    };
    document.getElementById("mk-clear").onclick = async () => {
      const before = S.marks.length;
      S.marks = S.marks.filter((m) => !(m.document_id === doc.id && m.page_no === S.pageNo));
      if (S.marks.length === before) return Toast("本页没有标注", true);
      await saveMarks();
      renderTabBody();
    };
    bar.querySelectorAll("[data-mode]").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === S.mode);
      b.onclick = () => { S.mode = b.dataset.mode; renderTabBody(); };
    });

    window.PdfView.renderPage(doc.id, S.pageNo, canvas, S.scale).then((info) => {
      S.pageCount = info.pageCount;
      const pg = document.getElementById("mk-page");
      if (pg) pg.textContent = `第 ${S.pageNo} / ${info.pageCount} 页`;
      paintMarks(overlay, doc);
      paintNotes(noteBody, doc, overlay, svg);
    }).catch((e) => {
      viewerWrap.innerHTML = `<div class="empty">材料原页渲染失败：${esc(e.message)}<br>`
        + `<span class="hint">目前只有 PDF 材料支持原页渲染与标注。</span></div>`;
    });

    bindDraw(overlay, doc);
  }

  /** 把当前页的标注画到覆盖层。 */
  function paintMarks(overlay, doc) {
    overlay.innerHTML = "";
    overlay.style.cursor = S.mode === "view" ? "default" : "crosshair";
    S.marks
      .filter((m) => m.document_id === doc.id && m.page_no === S.pageNo)
      .forEach((m) => {
        const box = el("div", "mk-mark " + (m.kind === "circle" ? "circle" : "hl"));
        box.style.left = m.x * 100 + "%";
        box.style.top = m.y * 100 + "%";
        box.style.width = m.w * 100 + "%";
        box.style.height = m.h * 100 + "%";
        box.dataset.idx = String(S.marks.indexOf(m));
        box.title = m.text || "（还没有旁注）";
        overlay.appendChild(box);
      });
    if (S.drag) {
      const d = el("div", "mk-mark hl drag");
      d.style.left = Math.min(S.drag.x0, S.drag.x1) * 100 + "%";
      d.style.top = Math.min(S.drag.y0, S.drag.y1) * 100 + "%";
      d.style.width = Math.abs(S.drag.x1 - S.drag.x0) * 100 + "%";
      d.style.height = Math.abs(S.drag.y1 - S.drag.y0) * 100 + "%";
      overlay.appendChild(d);
    }
  }

  /** 右侧旁注列表 + 连线。 */
  function paintNotes(noteBody, doc, overlay, svg) {
    noteBody.innerHTML = "";
    const pageMarks = S.marks
      .map((m, idx) => ({ m, idx }))
      .filter((x) => x.m.document_id === doc.id && x.m.page_no === S.pageNo);
    const head = noteBody.parentElement.querySelector(".panel-head");
    if (head) head.innerHTML = `<span>旁注（${pageMarks.length}）</span>`;

    if (!pageMarks.length) {
      noteBody.appendChild(el("div", "empty",
        "本页还没有标注。<br>选「高亮」或「圈注」后在材料上拖一下。"));
      drawLinks([], overlay, svg);
      return;
    }
    const items = [];
    pageMarks.forEach(({ m, idx }, order) => {
      const card = el("div", "note-card");
      card.innerHTML = `<div class="row" style="justify-content:space-between">
          <span class="pill ${m.kind === "circle" ? "warn" : ""}">${
            m.kind === "circle" ? "圈注" : "高亮"} ${order + 1}</span>
          <span class="hint">${m.source === "model" ? "AI 标注" : "我的标注"}</span>
        </div>`;
      const ta = el("textarea", "note-input");
      ta.rows = 2;
      ta.placeholder = "写下这条旁注（自动保存）";
      ta.value = m.text || "";
      ta.oninput = () => { m.text = ta.value; scheduleSave(); };
      card.appendChild(ta);
      const del = el("button", "btn small danger", "删除");
      del.onclick = async () => {
        S.marks.splice(idx, 1);
        await saveMarks();
        renderTabBody();
      };
      card.appendChild(del);
      noteBody.appendChild(card);
      items.push({ el: card, mark: m });
    });
    requestAnimationFrame(() => drawLinks(items, overlay, svg));
  }

  /** 用二次贝塞尔把标注框与旁注卡片连起来（「拉出弧线写讲解」）。 */
  function drawLinks(items, overlay, svg) {
    const layout = svg.parentElement;
    if (!layout) return;
    const base = layout.getBoundingClientRect();
    const stage = overlay.parentElement;
    if (!stage) return;
    const stageBox = stage.getBoundingClientRect();
    svg.setAttribute("width", String(base.width));
    svg.setAttribute("height", String(base.height));
    svg.setAttribute("viewBox", `0 0 ${base.width} ${base.height}`);
    svg.innerHTML = "";
    if (!items.length) return;
    items.forEach(({ el: cardEl, mark }) => {
      const box = overlay.querySelector(`[data-idx="${S.marks.indexOf(mark)}"]`);
      if (!box) return;
      const b = box.getBoundingClientRect();
      const c = cardEl.getBoundingClientRect();
      const x1 = b.right - base.left;
      const y1 = b.top - base.top + b.height / 2;
      const x2 = c.left - base.left;
      const y2 = c.top - base.top + 16;
      if (x2 <= x1 + 2) return;   // 旁注不在右侧时不画线
      const cx = (x1 + x2) / 2;
      svg.insertAdjacentHTML("beforeend",
        `<path d="M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}"`
        + ` fill="none" stroke="var(--primary)" stroke-width="1.2" opacity="0.55"/>`
        + `<circle cx="${x1}" cy="${y1}" r="3" fill="var(--primary)" opacity="0.7"/>`);
    });
    // stageBox 仅用于保持引用（避免未使用变量告警）
    void stageBox;
  }

  /** 在材料上拖拽画标注。 */
  function bindDraw(overlay, doc) {
    const rel = (ev) => {
      const r = overlay.getBoundingClientRect();
      return {
        x: Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)),
        y: Math.max(0, Math.min(1, (ev.clientY - r.top) / r.height)),
      };
    };
    overlay.onmousedown = (ev) => {
      if (S.mode === "view") return;
      ev.preventDefault();
      const p = rel(ev);
      S.drag = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      overlay.onmousemove = (e2) => {
        const q = rel(e2);
        S.drag.x1 = q.x;
        S.drag.y1 = q.y;
        paintMarks(overlay, doc);
      };
      const finish = async () => {
        document.removeEventListener("mouseup", finish);
        overlay.onmousemove = null;
        const d = S.drag;
        S.drag = null;
        if (!d) return;
        const x = Math.min(d.x0, d.x1);
        const y = Math.min(d.y0, d.y1);
        const w = Math.abs(d.x1 - d.x0);
        const h = Math.abs(d.y1 - d.y0);
        if (w < 0.01 || h < 0.01) { paintMarks(overlay, doc); return; }
        S.marks.push({
          document_id: doc.id, page_no: S.pageNo, kind: S.mode,
          x, y, w, h, text: "", source: "user",
        });
        await saveMarks();
        renderTabBody();
      };
      document.addEventListener("mouseup", finish);
    };
  }

  /** 保存标注（输入时防抖；增删立即保存）。 */
  function scheduleSave() {
    clearTimeout(S.saveTimer);
    S.saveTimer = setTimeout(saveMarks, 800);
  }

  async function saveMarks() {
    try {
      const r = await Api.put("/api/courses/lessons/" + S.lesson.id + "/marks",
        { marks: S.marks });
      S.marks = r.marks || S.marks;
      const box = document.getElementById("mk-status");
      if (box) {
        box.textContent = "标注已保存";
        setTimeout(() => { if (box.textContent === "标注已保存") box.textContent = ""; }, 1500);
      }
    } catch (e) {
      Toast("标注保存失败：" + e.message, true);
    }
  }

  /* ── 页签 3：单元总结 ─────────────────── */
  function renderSummary(host) {
    const unit = S.unit;
    if (!unit) {
      host.appendChild(el("div", "empty", "找不到所属单元。"));
      return;
    }
    // 已生成过但本地还没取过（例如从课程页直接跳进来）：补一次拉取，导出才有内容。
    if (!S.unitSummary && unit.summary_status === "ready") {  // 已生成过但还没取到
      Api.get(`/api/courses/units/${unit.id}/summary`)
        .then((d) => { S.unitSummary = d; renderTabBody(); })
        .catch(() => { /* 拉取失败就按课程页带下来的状态展示 */ });
    }
    const lessons = unit.lessons || [];
    const done = lessons.every((l) => l.status === "done");
    const status = (S.unitSummary && S.unitSummary.status) || unit.summary_status || "pending";
    const card = el("div", "card");
    card.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b>第 ${unit.ordinal} 单元 · ${esc(unit.title)}</b>
        <span class="pill ${status === "ready" ? "ok" : status === "failed" ? "bad" : ""}">${
          status === "ready" ? "已生成" : status === "running" ? "生成中"
            : status === "failed" ? "生成失败" : "未生成"}</span>
      </div>
      ${unit.summary ? `<div class="hint">${esc(unit.summary)}</div>` : ""}
      <div class="hint">本单元共 ${lessons.length} 节，已完成 ${
        lessons.filter((l) => l.status === "done").length} 节。</div>
      <div class="row" style="margin-top:10px">
        <button class="btn small primary" id="sum-gen">${
          status === "ready" ? "重新生成总结" : "生成单元总结"}</button>
        <button class="btn small" id="sum-export"${status === "ready" ? "" : " disabled"}>导出总结</button>
        <span class="hint" id="sum-stage">${
          (S.unitSummary && S.unitSummary.error) ? esc(S.unitSummary.error) : ""}</span>
      </div>
      ${done ? "" : '<div class="hint" style="color:var(--warn)">建议先完成本单元全部讲次，总结会更准确。</div>'}`;
    host.appendChild(card);

    const data = (S.unitSummary && S.unitSummary.summary) || unit.summary_data;
    if (data) {
      if (data.recap) host.appendChild(el("div", "board-summary", esc(data.recap)));
      const grid = el("div", "board-grid");
      [["已掌握", data.mastered, ""], ["待巩固", data.weak_points, "warn"],
       ["下一步", data.next_steps, ""]].forEach(([name, items, tone]) => {
        if (!items || !items.length) return;
        const box = el("div", "board-card");
        box.appendChild(el("div", "card-kind", name));
        const ul = el("ul");
        ul.style.margin = "4px 0 0";
        ul.style.paddingLeft = "18px";
        items.forEach((x) => {
          const li = el("li", null, esc(x));
          if (tone === "warn") li.style.color = "var(--warn)";
          ul.appendChild(li);
        });
        box.appendChild(ul);
        grid.appendChild(box);
      });
      host.appendChild(grid);
      if (data.score_note) {
        const box = el("div", "board-sec");
        box.appendChild(el("div", "sec-title", "练习表现"));
        box.appendChild(el("div", "hint", esc(data.score_note)));
        host.appendChild(box);
      }
      const stats = data.stats;
      if (stats) {
        host.appendChild(el("div", "hint",
          `统计：讲次 ${stats.lessons} · 题目 ${stats.questions} · 得分 ${stats.score} · 错题 ${stats.errors}`));
      }
    } else if (status !== "running") {
      host.appendChild(el("div", "empty",
        "还没有单元总结。<br>点上方按钮生成，会结合本单元的练习表现给出薄弱点与下一步建议。"));
    }

    document.getElementById("sum-gen").onclick = async () => {
      const btn = document.getElementById("sum-gen");
      btn.disabled = true;
      const stage = document.getElementById("sum-stage");
      try {
        const r = await Api.post(`/api/courses/units/${unit.id}/summary`);
        await pollJob(r.job_id, (s) => { if (stage) stage.textContent = s || ""; });
        S.unitSummary = await Api.get(`/api/courses/units/${unit.id}/summary`);
        Toast("单元总结已生成");
        renderTabBody();
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false;
      }
    };
    document.getElementById("sum-export").onclick = () => {
      const md = (S.unitSummary && S.unitSummary.markdown) || "";
      if (!md) return Toast("还没有总结内容", true);
      BoardExport.downloadText(md, `单元总结-${unit.title}.md`);
    };
  }

  /* ── 引用 / 课堂提问 ───────────────────── */
  function renderCitations() {
    const box = document.getElementById("lesson-cites");
    if (!box) return;
    box.innerHTML = "";
    const cites = (S.lesson && S.lesson.citations) || [];
    // 默认收起：右侧高度优先给「讲师讲述」与课堂提问，需要核对出处时再展开。
    box.style.flex = S.citesOpen ? "1 1 40%" : "0 0 auto";

    const head = el("div", "panel-head clickable");
    head.innerHTML = `<span>引用来源（${cites.length}）</span>`
      + `<span class="fold">${S.citesOpen ? "▾ 收起" : "▸ 展开"}</span>`;
    head.title = S.citesOpen ? "点击收起" : "点击展开查看引用出处";
    head.onclick = () => { S.citesOpen = !S.citesOpen; renderCitations(); };
    box.appendChild(head);

    if (!S.citesOpen) return;

    const body = el("div", "panel-body");
    if (!cites.length) body.appendChild(el("div", "empty", "讲义中的材料引用会显示在这里"));
    cites.forEach((c) => {
      const snip = String(c.snippet || "").replace(/\s+/g, " ").trim();
      const card = el("div", "cite-card",
        `<div class="n">[${c.n}] ${c.page_no != null ? "第 " + c.page_no + " 页" : ""}</div>
         <div class="src">《${esc(c.document_title)}》${c.section ? " · " + esc(c.section) : ""}</div>
         <div class="snip">${esc(snip.slice(0, 90))}</div>`);
      if (c.document_id && c.page_no != null) {
        card.style.cursor = "pointer";
        card.title = "在材料中查看这一页";
        card.onclick = () => {
          jumpToMark({ document_id: c.document_id, page_no: c.page_no });
          S.tab = "marks";
          renderTabs(); renderTabBody();
        };
      }
      body.appendChild(card);
    });
    box.appendChild(body);
  }

  // 课堂提问发给后端时会带上这段上下文（课程/讲次/当前课件页），
  // 让「这个一样吗」「这里为什么」这类指代问题能检索到本讲材料；
  // 展示时再用 CTX_RE 把前缀剥掉，对话界面保持干净。
  const CTX_RE = /^\[课堂上下文：[^\]]*\]\s*/;
  function lessonContext() {
    const bits = [];
    if (S.course && S.course.title) bits.push(`课程《${S.course.title}》`);
    if (S.lesson && S.lesson.title) bits.push(`讲次「${S.lesson.title}」`);
    if (S.slides.length) {
      const idx = Math.min(Math.max(S.slideIndex, 0), S.slides.length - 1);
      const sl = S.slides[idx];
      if (sl) bits.push(`当前课件第 ${idx + 1} 页「${sl.title}」`);
    }
    return bits.join("，");
  }

  function renderChat() {
    const sc = document.getElementById("lesson-chat");
    if (!sc) return;
    sc.innerHTML = "";
    if (!S.messages.length) {
      sc.appendChild(el("div", "empty", "对这一节有疑问？直接在下面输入，回答只依据你的材料。"));
    }
    S.messages.forEach((m) => {
      const wrap = el("div", "msg " + (m.role === "user" ? "user" : "assistant"));
      wrap.appendChild(el("div", "who", m.role === "user" ? "我" : "知伴"));
      const bubble = el("div", "bubble md");
      if (m.role === "user") {
        bubble.textContent = String(m.content || "").replace(CTX_RE, "");
      } else {
        MD.mount(bubble, m.content || "");
      }
      wrap.appendChild(bubble);
      (m.citations || []).forEach((c) => wrap.appendChild(el("div", "cite-card",
        `<div class="n">[${c.n}] ${c.page_no != null ? "第 " + c.page_no + " 页" : ""}</div>
         <div class="src">《${esc(c.document_title)}》</div>
         <div class="snip">${esc(String(c.snippet || "").slice(0, 80))}</div>`)));
      sc.appendChild(wrap);
    });
    sc.scrollTop = sc.scrollHeight;
  }

  async function send() {
    if (S.streaming) { if (S.abort) S.abort.abort(); return; }
    const ta = document.getElementById("lesson-input");
    const text = ta.value.trim();
    if (!text) return;
    const badge = document.getElementById("model-badge");
    if (!badge.dataset.ok) return Toast("请先在「设置」里配置对话模型", true);

    ta.value = "";
    S.streaming = true;
    const sc = document.getElementById("lesson-chat");
    sc.appendChild(el("div", "msg user",
      `<div class="who">我</div><div class="bubble">${esc(text)}</div>`));
    const live = el("div", "msg assistant");
    live.appendChild(el("div", "who", "知伴"));
    const bubble = el("div", "bubble md", '<span class="hint">思考中…</span>');
    live.appendChild(bubble);
    sc.appendChild(live);
    sc.scrollTop = sc.scrollHeight;

    let acc = "";
    const ctx = lessonContext();
    S.abort = Api.stream("/api/chat/stream", {
      conversation_id: S.conversationId,
      message: (ctx ? `[课堂上下文：${ctx}] ` : "") + text,
      guided: false,
      document_ids: (S.course && S.course.document_ids) || null,
      // 课堂提问默认允许在材料之外补充：strict 模式会在无命中时直接回「材料中未提及」，
      // 导致「linux 系统也是一样吗」这类延伸问题被拒答；loose 模式由服务端在无引用时
      // 自动加「（材料外回答，未基于当前材料）」前缀，行为更友好。
      grounding: "loose",
    }, {
      delta: (d) => { acc += d.text; MD.mount(bubble, acc); sc.scrollTop = sc.scrollHeight; },
      citation: (d) => { if (d.content) { acc = d.content; MD.mount(bubble, acc); } },
      error: (d) => {
        MD.mount(bubble, (acc || "") + `\n\n> ⚠️ ${d.message || "调用失败"}`);
        Toast(d.message || "调用失败", true);
      },
      close: async () => {
        S.streaming = false;
        try {
          const conv = await Api.get("/api/conversations/" + S.conversationId);
          S.messages = conv.messages || [];
          renderChat();
        } catch (e) { /* 忽略刷新失败 */ }
      },
    });
  }

  /* ── 页签装配 ─────────────────────────── */
  /** 当前讲次**可见**的页签。
   *
   * 练习讲次没有课件/讲义，只留「练习 / 单元总结」——四个页签里空三个会让用户
   * 以为页面坏了（用户反馈：点进练习讲次「页面什么也没有」）。
   */
  function visibleTabs() {
    return (S.lesson && S.lesson.kind === "practice")
      ? [["practice", "练习"], ["summary", "单元总结"]]
      : [["slides", "课件"], ["board", "讲义"], ["marks", "材料标注"], ["summary", "单元总结"]];
  }

  function renderTabs() {
    const bar = document.getElementById("lesson-tabs");
    if (!bar) return;
    bar.innerHTML = "";
    visibleTabs().forEach(([key, label]) => {
      const b = el("button", S.tab === key ? "active" : "", label);
      b.onclick = () => { S.tab = key; renderTabs(); renderTabBody(); };
      bar.appendChild(b);
    });
    const right = el("span", "hint");
    right.id = "mk-status";
    bar.appendChild(right);
  }

  function renderTabBody() {
    const host = document.getElementById("tab-body");
    if (!host) return;
    host.innerHTML = "";
    if (S.tab === "slides") renderSlides(host);
    else if (S.tab === "board") renderBoard(host);
    else if (S.tab === "marks") renderMarks(host);
    else if (S.tab === "practice") renderPractice(host);
    else renderSummary(host);
  }

  /* ── 导出 / 朗读 ──────────────────────── */
  function exportBoardPng() {
    if (!S.lesson.board) return Toast("还没有讲义，先生成讲义", true);
    BoardExport.downloadPng(S.lesson.board,
      `讲义-${S.lesson.title}.png`, { title: S.lesson.title, width: 900 });
    Toast("正在导出讲义图片…");
  }

  function exportBoardMd() {
    if (!S.lesson.board_md) return Toast("还没有讲义内容", true);
    BoardExport.downloadText(
      `# ${S.lesson.title}\n\n> 目标：${S.lesson.objective || ""}\n\n${S.lesson.board_md}\n`,
      `讲义-${S.lesson.title}.md`);
  }

  async function exportConversation() {
    try {
      const resp = await fetch(`/api/courses/lessons/${S.lesson.id}/export?kind=conversation`);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const text = await resp.text();
      BoardExport.downloadText(text, `课堂对话-${S.lesson.title}.md`);
    } catch (e) {
      Toast("导出对话失败：" + e.message, true);
    }
  }

  function normaliseSlide(raw, idx) {
    raw = raw || {};
    const bullets = Array.isArray(raw.bullets)
      ? raw.bullets.map((x) => String(x || "").trim()).filter(Boolean)
      : [];
    return {
      id: raw.id || ("slide-" + (idx + 1)),
      kind: raw.kind || "note",
      title: raw.title || "课件页",
      bullets,
      body: raw.body || "",
      // 可视化页透传（后端契约：diagram/chart/table/takeaway 仅在对应 kind 时存在）
      diagram: (raw.kind === "diagram" && raw.diagram) ? raw.diagram : null,
      chart: (raw.kind === "chart" && raw.chart) ? raw.chart : null,
      table: (raw.kind === "table" && raw.table) ? raw.table : null,
      takeaway: (raw.kind === "takeaway" && raw.takeaway) ? raw.takeaway : null,
    };
  }

  /** 读取「学生课件页」：优先使用新结构，旧数据才从讲义卡片派生。 */
  function buildSlides() {
    const b = (S.lesson && S.lesson.board) || {};
    const raw = (Array.isArray(S.lesson && S.lesson.slides) && S.lesson.slides.length)
      ? S.lesson.slides : (Array.isArray(b.slides) ? b.slides : []);
    if (raw.length) return raw.map((x, i) => normaliseSlide(x, i));

    // 兼容旧课程：没有 slides/scripts 字段时，仍可从 board.cards 临时派生。
    const out = [];
    if (b.summary) out.push({ id: "slide-1", kind: "concept", title: "本讲导览", bullets: [b.summary], body: "" });
    (b.cards || []).forEach((c, i) => {
      const body = String(c.body || "");
      const bullets = body.split(/[。；;\n]+/).map((x) => x.trim()).filter(Boolean).slice(0, 3);
      out.push({ id: "slide-" + (out.length + 1), kind: c.kind, title: c.title || "", bullets, body: "" });
      void i;
    });
    if (b.recap) out.push({ id: "slide-" + (out.length + 1), kind: "note", title: "本讲回顾", bullets: [b.recap], body: "" });
    return out;
  }

  function slideDisplayText(sl) {
    return [sl.title].concat(sl.bullets || [], sl.body ? [sl.body] : [])
      .filter(Boolean).join("。");
  }

  /** P3 本讲导览：把 lesson.outline / lesson.keypoints 拼成两级 markdown（markmap 消费）。 */
  function outlineMarkdown() {
    const out = (S.lesson && Array.isArray(S.lesson.outline) ? S.lesson.outline : [])
      .map((o) => String(o == null ? "" : o).trim()).filter(Boolean)
      .map((o) => "- " + o);
    const kps = (S.lesson && Array.isArray(S.lesson.keypoints) ? S.lesson.keypoints : [])
      .map((k) => {
        if (!k) return "";
        const t = String((k.term == null ? "" : k.term)).trim();
        const d = String((k.desc == null ? "" : k.desc)).trim();
        if (!t) return "";                 // 空的子项跳过（避免脏数据撑出空节点）
        return "  - " + t + (d ? "：" + d : "");
      }).filter(Boolean);
    return out.concat(kps).join("\n");
  }

  /** 读取「讲师讲稿」：优先使用新结构，旧数据才按课件生成兜底讲稿。 */
  function buildScripts() {
    const b = (S.lesson && S.lesson.board) || {};
    const raw = (Array.isArray(S.lesson && S.lesson.scripts) && S.lesson.scripts.length)
      ? S.lesson.scripts : (Array.isArray(b.scripts) ? b.scripts : []);
    const ids = new Set(S.slides.map((s) => s.id));
    const byId = new Map();
    raw.forEach((x) => {
      if (!x || !ids.has(x.slide_id) || !x.text || byId.has(x.slide_id)) return;
      byId.set(x.slide_id, { slide_id: x.slide_id, text: String(x.text || ""), cue: x.cue || "" });
    });
    return S.slides.map((sl) => byId.get(sl.id) || {
      slide_id: sl.id,
      text: slideDisplayText(sl),
      cue: "legacy-fallback",
    });
  }

  function scriptTextForSlide(sl, idx) {
    const hit = (S.scripts || []).find((x) => x.slide_id === sl.id) || (S.scripts || [])[idx];
    return (hit && hit.text) || slideDisplayText(sl);
  }

  /* ── 课件页渲染（按讲授进度逐步出现）────── */
  function renderSlides(host) {
    if (!S.slides.length) { S.slides = buildSlides(); S.scripts = buildScripts(); }
    const wrap = el("div", "board slides");
    if (!S.slides.length) {
      // 没有课件：分两种情况给出可操作的引导，而不是一句冷冰冰的空提示。
      if (S.lesson && S.lesson.kind === "practice") {
        const empty = el("div", "empty card");
        empty.innerHTML = "这一节是<b>随堂练习</b>，没有课件内容。<br>"
          + "点下面的按钮，即可开始答题。";
        const go = el("button", "btn primary", "开始练习");
        go.style.marginTop = "14px";
        go.onclick = () => startPractice(go);
        const holder = el("div");
        holder.appendChild(go);
        empty.appendChild(holder);
        wrap.appendChild(empty);
      } else {
        const empty = el("div", "empty card");
        empty.innerHTML = "这一节还没有课件内容。<br>"
          + "点下面的按钮，系统会依据你的材料生成本讲的课件与讲义。";
        const go = el("button", "btn primary", "生成讲义，开始学习");
        go.style.marginTop = "14px";
        go.onclick = () => startLecture(go);
        const holder = el("div");
        holder.appendChild(go);
        empty.appendChild(holder);
        wrap.appendChild(empty);
      }
      host.appendChild(wrap);
      return;
    }
    // 讲授中：只显示已讲到的页；不在讲授中：整份课件都可翻看。
    const shown = S.teaching ? Math.max(0, S.slideIndex) : S.slides.length - 1;
    const total = S.slides.length;

    // P3 本讲导览：不讲授时，于课件最前插入一张 markmap 导览卡。
    // 讲授中(S.teaching)不显示，避免干扰自动翻页；markmap 失败则整卡移除。
    if (!S.teaching) {
      const md = outlineMarkdown();
      if (md && window.MD && typeof window.MD.mindmap === "function") {
        const oCard = el("div", "card lesson-outline-card");
        oCard.appendChild(el("div", "card-kind", "本讲导览"));
        const oBox = el("div");
        oCard.appendChild(oBox);
        wrap.appendChild(oCard);
        try {
          const svg = window.MD.mindmap(oBox, md);
          if (!svg) oCard.remove();
        } catch (e) {
          oCard.remove();
        }
      }
    }

    const bar = el("div", "slide-bar");
    bar.innerHTML = S.teaching
      ? `<span>课件 ${Math.min(shown + 1, total)} / ${total}</span><span class="hint">跟随讲授进度自动翻页</span>`
      : `<span>课件 ${total} 页</span><span class="hint">点「▶ 开始上课」按顺序讲授</span>`;
    wrap.appendChild(bar);

    for (let i = 0; i <= shown && i < total; i++) {
      const sl = S.slides[i];
      const [name, bg] = KIND[sl.kind] || KIND.note;
      // 讲授中：已讲过的页加 past 弱化（半透明 + 收起正文），当前页保持高亮聚焦
      const card = el("div", "board-card slide"
        + (i === shown ? " current" : "")
        + (S.teaching && i < shown ? " past" : ""));
      card.style.background = bg;
      card.id = "slide-" + i;
      card.appendChild(el("div", "card-kind", name + " · 第 " + (i + 1) + " 页"));
      if (sl.title) card.appendChild(el("div", "card-title", esc(sl.title)));
      // P1/P2 可视化页：title 之后、bullets 之前插入渲染容器，交给 Viz 渲染。
      // 失败回退由 Viz 内部放置 .viz-fallback（bullets 仍照常渲染，不归此处管）。
      // 图示页现在有两种形态：后端已编译好的 svg（主）与历史数据的 mermaid code（兼容），
      // 任一存在就要交给 Viz 渲染 —— 之前这里只认 code，导致新形态的图整页不渲染。
      if (window.Viz && typeof window.Viz.render === "function"
          && ((sl.kind === "diagram" && sl.diagram && (sl.diagram.svg || sl.diagram.code))
              || (sl.kind === "chart" && sl.chart))) {
        const vbox = el("div", "viz-box");
        card.appendChild(vbox);
        window.Viz.render(vbox, sl);
      }
      // P1 特色页：对比表格 / 金句卡（纯 HTML，由 Viz 渲染；结构非法则自动跳过，
      // bullets 照常渲染，学生不会看到空白）
      if (window.Viz && typeof window.Viz.renderExtras === "function") {
        window.Viz.renderExtras(card, sl);
      }
      if (sl.bullets && sl.bullets.length) {
        const ul = el("ul", "card-body");
        // 要点按**行内 Markdown** 渲染：提示词引导模型把关键术语/编号用加粗标出，
        // 原先 esc() 纯文本会把 `**936**` 的星号原样显示给学生。
        sl.bullets.forEach((x) => ul.appendChild(el("li", null, mdInline(x))));
        card.appendChild(ul);
      }
      if (sl.body) {
        const body = el("div", "card-body md");
        MD.mount(body, sl.body);
        card.appendChild(body);
      }
      wrap.appendChild(card);
    }
    host.appendChild(wrap);
    // 自动把当前页**顶边**对齐滚动容器顶部：新页从标题开始完整显示。
    //
    // 弃用 scrollIntoView —— 它按「视口」对齐，而底部字幕条（#teach-stage，
    // position:fixed）盖在视口底之上；`body.teaching-on #tab-body` 的
    // padding-bottom 只增大滚动总高度、**不参与**对齐计算，于是页底永远藏在
    // 字幕后面（用户实测：新页被遮住一半）。顶对齐天然不受底部遮挡影响。
    if (S.teaching) {
      const cur = document.getElementById("slide-" + shown);
      const box = document.querySelector(".lesson-head");
      if (cur && box) {
        // 容器顶部还压着 sticky 操作条（.lesson-bar），偏移量要加上它的高度，
        // 否则只是把「被字幕遮」换成「被操作条遮」。
        const bar = box.querySelector(".lesson-bar");
        const pad = (bar ? bar.offsetHeight : 0) + 8;
        // 用 rect 差值而非 offsetTop：不依赖 offsetParent 链，容器自身是否定位都成立
        const delta = cur.getBoundingClientRect().top - box.getBoundingClientRect().top;
        box.scrollTo({ top: Math.max(0, box.scrollTop + delta - pad), behavior: "smooth" });
      }
    }
  }

  /* ── 页签：练习（practice 讲次专用）────────────── */
  /** 练习讲次的「练习」页签正文：一张引导卡片，主按钮复用 startPractice()。 */
  function renderPractice(host) {
    const l = S.lesson;
    const wrap = el("div", "board practice-intro");
    const card = el("div", "card");
    let html = `<div class="card-title" style="font-size:18px;margin-bottom:6px">这是一节随堂练习</div>`
      + `<div class="hint">练习题由 AI 依据本单元前面的讲次与你的材料生成。</div>`;
    if (l.objective) {
      html += `<div class="hint" style="margin-top:6px">学习目标：${esc(l.objective)}</div>`;
    }
    card.innerHTML = html;
    const row = el("div", "row");
    row.style.marginTop = "14px";
    const start = el("button", "btn primary", S.questionCount ? "继续练习" : "开始练习");
    start.onclick = () => startPractice(start);
    row.appendChild(start);
    if (S.questionCount) {
      const cont = el("button", "btn small", "查看 / 继续答题");
      cont.onclick = () => { location.hash = "#/practice/" + l.id; };
      row.appendChild(cont);
    }
    card.appendChild(row);
    wrap.appendChild(card);
    host.appendChild(wrap);
  }

  /* ── 屏幕中下方「授课舞台」：字幕 ── */
  /* 暂停式互动检查点（「听到这里，还好吗？」及其四个选项）已按用户要求
   * 于 2026-09-14 **永久移除**：讲授改为逐页连续进行，不再中途暂停。
   * 选择题/互动题类互动保留在随堂练习里。 */

  /**
   * 重画授课舞台：字幕常显（默认开启，不提供关闭入口）。
   */
  function renderStage() {
    const stage = document.getElementById("teach-stage");
    if (!stage) return;
    const visible = !!(S.teaching || S.finished);
    stage.hidden = !visible;
    // 上课时给课件区留出字幕高度（见 app.css 的 body.teaching-on #tab-body）
    document.body.classList.toggle("teaching-on", visible);
    if (!visible) return;
    const sub = document.getElementById("teach-sub");
    if (!sub) return;
    // 字幕文本优先级：主动暂停提示 > 正在朗读的句子 > 已讲完提示 > 待开始提示
    const text = S.voicePaused ? "已暂停——点这里继续上课"
      : (S.subtitle || (S.finished ? "本讲讲完了。" : ""));
    sub.textContent = text || "准备开始…";
    // 暂停时整条字幕就是「继续」按钮：点字幕条任意位置即恢复播放
    // （用户反馈：单独冒出一个提示条还要去找顶栏的 ▶ 继续，太绕）。
    sub.onclick = S.voicePaused ? () => {
      S.voicePaused = false;
      Voice.resume();
      // 只重画动作按钮（▶/⏸ 标签），**不能调 renderHead()**——它会重建
      // #lesson-head（含一个空的 #tab-body 占位），把课件区整个清空，
      // 要等本页讲完 nextSlide() 里 renderTabBody() 才恢复 —— 这正是
      // 「点字幕恢复后课件消失约 30 秒」的根因（顶部 ▶ 按钮只调
      // renderStage()，所以一直正常）。
      renderActions();
      renderStage();
    } : null;
    sub.style.cursor = S.voicePaused ? "pointer" : "";
    sub.title = S.voicePaused ? "点击继续上课" : "";
  }

  /* ── 右侧「讲师讲述」：历史可上翻 + 完成后下一步 ── */
  function renderSpeaking() {
    const box = document.getElementById("lesson-speaking");
    if (!box) return;
    box.innerHTML = "";
    const status = S.teaching ? "● 进行中" : (S.finished ? "✔ 已讲完" : "待开始");
    box.appendChild(el("div", "panel-head",
      `<span>讲师讲述（${S.speakLog.length} 段）</span><span class="fold">${status}</span>`));
    const body = el("div", "panel-body");
    body.style.maxHeight = "280px";
    body.style.overflowY = "auto";

    if (!S.teaching && !S.speakLog.length && !S.finished) {
      body.appendChild(el("div", "empty", "点「▶ 开始上课」后，这里会实时显示我正在讲的内容，讲过的段落都会保留，可以往上翻。"));
    } else {
      // 历史段：已讲完的每页讲稿，按页排列、可上翻
      S.speakLog.forEach((it) => {
        const item = el("div", "speak-item");
        item.appendChild(el("div", "who", `第 ${it.no} 页 · ${esc(it.title || "")}`));
        item.appendChild(el("div", "speak-text", esc(it.text)));
        body.appendChild(item);
      });
      // 当前正在讲的（高亮）
      if (S.teaching && S.speaking) {
        const cur = el("div", "speak-item live");
        cur.appendChild(el("div", "who", "正在讲"));
        cur.appendChild(el("div", "speak-text", esc(S.speaking)));
        body.appendChild(cur);
      }
    }

    // 互动检查点已移到屏幕中下方的授课舞台（见 renderStage）：
    // 放在右栏角落用户看不见 —— 选择必须出现在视线正前方。

    // 讲完后：给出明确的下一步，而不是只留一个输入框
    if (S.finished) {
      const card = el("div", "teach-card");
      card.appendChild(el("div", "t", "这一讲讲完了，接下来可以："));
      const row = el("div", "row");
      const quiz = el("button", "btn small primary",
        S.questionCount ? "📝 去测验（5 题）" : "📝 生成本讲测验（5 题）");
      quiz.onclick = () => startPractice(quiz);
      const ask = el("button", "btn small", "❓ 还有疑问");
      ask.onclick = () => { S.finished = false; renderSpeaking(); const ta = document.getElementById("lesson-input"); if (ta) ta.focus(); };
      const next = el("button", "btn small", "➡ 没有疑问，进入下一课");
      next.onclick = () => { const b = document.getElementById("b-done"); if (b) { b.click(); } };
      const outline = el("button", "btn small", "📚 返回课程大纲");
      outline.onclick = () => { location.hash = "#/courses"; };
      row.appendChild(quiz); row.appendChild(ask); row.appendChild(next); row.appendChild(outline);
      card.appendChild(row);
      body.appendChild(card);
    }

    box.appendChild(body);
    // 讲授中自动滚到底（最新一段可见）；暂停/讲完时不打扰用户上翻
    if (S.teaching) body.scrollTop = body.scrollHeight;
  }

  /** 一页讲稿读完后的分流：最后一页 → 讲完；否则直接下一页（连续讲授，不再暂停）。 */
  function afterSlideSpoken() {
    if (!S.teaching) return;
    // 把刚讲完的这页收进历史（可上翻回看）
    const sl = S.slides[S.slideIndex];
    if (sl) {
      S.speakLog.push({
        no: S.slideIndex + 1, title: sl.title,
        text: Voice.plainText(scriptTextForSlide(sl, S.slideIndex)),
      });
    }
    const isLast = S.slideIndex >= S.slides.length - 1;
    if (isLast) { finishTeaching(); return; }
    nextSlide();
  }

  /**
   * 朗读一页讲稿：**逐句**驱动字幕，读完交给 :func:`afterSlideSpoken` 分流。
   *
   * ``chunkChars`` 让 Voice 按句子切块，``onChunk`` 因此落到「一句」粒度，
   * 屏幕中下方的字幕就能跟着语音一句句刷新。
   */
  function speakSlide(sl) {
    const script = scriptTextForSlide(sl, S.slideIndex);
    // 跨页预取：本页开播的同时，把**下一页**讲稿提前送进合成缓存。
    // 流水线是页内的，页间冷启动（等下一页第一段合成 2~3s）正是
    // 「连续几段后停顿、字幕显示准备开始」的主要来源——预热后页间零等待。
    const nxt = S.slides[S.slideIndex + 1];
    if (nxt) Voice.prime(scriptTextForSlide(nxt, S.slideIndex + 1), { chunkChars: SUB_CHARS });
    S.speaking = Voice.plainText(script);
    S.subtitle = "";
    renderSpeaking();
    renderStage();
    Voice.speak(script, {
      chunkChars: SUB_CHARS,
      onChunk: (t) => {
        if (!S.teaching) return;
        S.speaking = t;
        S.subtitle = t;
        renderSpeaking();
        renderStage();
      },
      onEnd: () => afterSlideSpoken(),
      onWarn: (m) => Toast(m, true),
      // 朗读失败（如无音频设备 / 语音包缺失）不应中断整堂课：保留授课进度与
      // 字幕，仅提示。逐页推进由 onEnd 控制；否则无声音环境下「开始上课」
      // 会立刻被 onError 误判为失败而停课。
      onError: (m) => { Toast(m, true); },
    });
  }

  /** 逐页讲授：每页出现 → 朗读 → 读完走 afterSlideSpoken 分流。 */
  function nextSlide() {
    if (!S.teaching) return;
    S.slideIndex += 1;
    if (S.slideIndex >= S.slides.length) { finishTeaching(); return; }
    renderTabBody();
    speakSlide(S.slides[S.slideIndex]);
  }

  function finishTeaching() {
    S.voicePaused = false;
    Voice.resume();   // 清掉暂停标志，避免带着 _hold 卡死
    S.teaching = false;
    S.speaking = "";
    S.subtitle = "";
    S.finished = true;
    S.slideIndex = Math.max(0, S.slides.length - 1);
    renderHead();
    renderTabBody();
    renderSpeaking();
    renderStage();
    // 随堂测验不再等用户点：讲完就自动出题并进入答题。
    autoPractice();
  }

  /**
   * 讲完自动生成随堂测验并进入答题页。
   *
   * 用户反馈「当前需手动点击才出现」——这里改成讲完即自动生成（后台任务
   * + 字幕区播报进度），生成完直接跳到答题页；失败则给出可重试的提示，
   * 不会把用户卡在课堂上。
   */
  async function autoPractice() {
    const l = S.lesson;
    if (!l) return;
    const sub = document.getElementById("teach-sub");
    const say = (t) => { if (sub) sub.textContent = t; };
    try {
      if (!S.questionCount) {
        say("这一讲讲完了，正在为你生成随堂测验…");
        const r = await Api.post("/api/courses/lessons/" + l.id + "/practice", { count: 5 });
        await pollJob(r.job_id, (stage) => say("正在出题…" + (stage || "")));
        await loadLesson(l.id);
        renderTabBody();
        renderSpeaking();
      }
      // 仅在用户还在这一讲页面时才自动跳页：否则把人从别的页（闪卡/记忆/设置）
      // 拽走很糟糕，改为播报 + Toast 提示回到课堂即可开始。
      const onLesson = location.hash.startsWith("#/lessons/" + l.id)
        && !!document.getElementById("lesson-head");
      if (onLesson) {
        say("测验已生成，正在进入答题…");
        location.hash = "#/practice/" + l.id;
      } else {
        say("随堂测验已生成，回到课堂即可开始。");
        Toast("这一讲的随堂测验已生成", false);
      }
    } catch (e) {
      say("随堂测验生成失败：" + e.message + "（可在右侧「去测验」重试）");
      Toast("随堂测验生成失败：" + e.message, true);
    }
  }

  /**
   * 手动停止讲授：保留已讲进度，但**不**认作「讲完」。
   *
   * 与 :func:`finishTeaching` 的关键区别：不触发自动出题 —— 只有真正
   * 逐页讲到最后才算上完课。
   */
  function stopTeaching() {
    S.voicePaused = false;
    Voice.resume();   // 清掉暂停标志，避免带着 _hold 进入下一状态卡死
    Voice.stop();
    S.teaching = false;
    S.speaking = "";
    S.subtitle = "";
    renderHead();
    renderSpeaking();
    renderStage();
    updateReturnPill();
    Toast("已停止，可点「▶ 开始上课」从头再讲一遍");
  }

  function toggleSpeak(btn) {
    if (S.teaching || (window.speechSynthesis && speechSynthesis.speaking)) {
      stopTeaching();
      return;
    }
    if (!S.lesson.board) return Toast("还没有讲义，先生成讲义", true);
    startTeaching();
  }

  /* ── 装配 ─────────────────────────────── */
  /**
   * 主按钮：按「这一节当前最该做的事」决定。
   *
   * 讲次还没讲义时，最该做的是**生成讲义**——此前主按钮直接是「标记完成」，
   * 学生可以在没看过任何内容的情况下把整门课标记学完，进度就失真了。
   */
  function primaryAction(lesson) {
    if (lesson.kind === "practice") {
      return `<button class="btn small primary" id="b-practice">${
        S.questionCount ? "继续练习" : "生成随堂练习"}</button>`;
    }
    if (!lesson.board) {
      return '<button class="btn small primary" id="b-lecture-2">生成讲义，开始学习</button>';
    }
    return `<button class="btn small primary" id="b-done">${
      lesson.status === "done" ? "再学一次" : "跳过本节课，进入下一节"}</button>`;
  }

  /** 从 hash 查询串读取页签（如 #/lessons/xxx?tab=summary）。 */
  function initialTab() {
    const q = (location.hash.split("?")[1] || "");
    const m = /(?:^|&)tab=([a-z]+)/.exec(q);
    const tab = m ? m[1] : "";
    // 练习讲次只有「练习 / 单元总结」两个页签，默认落在「练习」。
    // 注意：本函数在 S.lesson 可用前也可能被调用（进入课堂时先置 tab 再拉数据），
    // 因此必须先用 S.lesson && ... 判空，否则会报错。
    if (S.lesson && S.lesson.kind === "practice") {
      return ["practice", "summary"].includes(tab) ? tab : "practice";
    }
    // 默认落在「课件」页签：这一页是上课时看的，讲义/标注/总结按需切换。
    return ["slides", "board", "marks", "summary"].includes(tab) ? tab : "slides";
  }

  /* ── 头部与动作按钮 ───────────────────── */
  /** 画讲次头部：标题 / 状态 / 动作按钮 / 页签容器 / 页签内容容器。 */
  /** 左上角位置标签：第 X 单元 · 第 Y 课 / 本单元共 N 课。 */
  function positionLabel() {
    const units = (S.course && S.course.units) || [];
    const ui = units.findIndex((u) => u.id === S.lesson.unit_id);
    if (ui < 0) return "";
    const lessons = units[ui].lessons || [];
    const li = lessons.findIndex((x) => x.id === S.lesson.id);
    if (li < 0) return "";
    return `第 ${ui + 1} 单元 · 第 ${li + 1} 课 / 本单元共 ${lessons.length} 课`;
  }

  /** 讲次教学设计 desc → 折叠展示块（与课程结构页同一格式；无 desc 返回空串）。 */
  function descHtml(d) {
    if (!d || typeof d !== "object") return "";
    const rows = [];
    if (d.outcomes) rows.push(`学习目标：${esc(d.outcomes.join("；"))}`);
    if (d.knowledge_points) rows.push(`知识点边界：${esc(d.knowledge_points.join("；"))}`);
    if (d.concepts) rows.push(`术语口径：${esc(d.concepts.join("、"))}`);
    if (d.operations) rows.push(`涉及操作：${esc(d.operations.join("；"))}`);
    const tr = d.transition || {};
    const seg = [];
    if (tr.prev) seg.push(`承接 ${esc(tr.prev)}`);
    if (tr.next) seg.push(`引向 ${esc(tr.next)}`);
    if (tr.avoid) seg.push(`避免展开 ${esc(tr.avoid)}`);
    if (seg.length) rows.push(`讲间衔接：${seg.join("；")}`);
    if (d.visual && d.visual !== "无") rows.push(`可视化提示：${esc(d.visual)}`);
    if (d.exercise_focus) rows.push(`考察点：${esc(d.exercise_focus.join("；"))}`);
    if (d.expected_mistakes) rows.push(`学生易错点：${esc(d.expected_mistakes.join("；"))}`);
    if (d.exercise_flow) rows.push(`题型安排：${esc(d.exercise_flow)}`);
    if (!rows.length) return "";
    return `<details class="lesson-desc"><summary>教学设计（AI 按此备课）</summary>
      <div class="hint" style="margin:6px 0 0">${rows.map((r) => `<div>· ${r}</div>`).join("")}</div></details>`;
  }

  function renderHead() {
    const l = S.lesson;
    const box = document.getElementById("lesson-head");
    if (!box) return;
    box.innerHTML = `
      <div class="lesson-bar">
        <button class="btn small" id="b-back" title="返回课程列表">← 返回课程</button>
        <span class="lesson-pos" id="lesson-pos">${esc(positionLabel())}</span>
        <b class="lesson-title">${esc(l.title)}</b>
        <span class="pill">${esc(l.kind_name)}</span>
        ${l.status === "done" ? '<span class="pill ok">已完成</span>' : ""}
        <div class="row lesson-bar-actions" id="lesson-actions"></div>
      </div>
      <div class="tabs" id="lesson-tabs"></div>
      <div class="hint">${esc(l.objective || "")}</div>
      ${descHtml(l.desc)}
      <div id="tab-body"></div>`;
    renderActions();
  }

  /** 动作按钮：随「有无讲义 / 有无题目」变化，所以单独可重画。 */
  function renderActions() {
    const l = S.lesson;
    const box = document.getElementById("lesson-actions");
    if (!box) return;
    // 没有课件可讲时（练习讲次 / 讲义尚未生成的讲次）就别摆出「开始上课 / 导出图片 /
    // 导出讲义」这三个点了没反应的按钮——练习讲次进去「看着像坏了」的观感就是这么来的。
    // buildSlides() 是纯函数（只读 S.lesson、无副作用），在这里调用是安全的。
    const hasSlides = buildSlides().length > 0;
    box.innerHTML = `
      ${hasSlides ? `<button class="btn small" id="b-speak">${S.teaching ? "⏹ 停止" : "▶ 开始上课"}</button>` : ""}
      ${S.teaching ? `<button class="btn small" id="b-pause">${S.voicePaused ? "▶ 继续" : "⏸ 暂停"}</button>` : ""}
      ${l.kind === "practice" ? "" : `<button class="btn small" id="b-lecture">${
        l.board ? "重新生成讲义" : "生成讲义"}</button>`}
      ${hasSlides ? '<button class="btn small" id="b-png">导出图片</button>' : ""}
      ${hasSlides ? '<button class="btn small" id="b-md">导出讲义</button>' : ""}
      <button class="btn small" id="b-conv">导出对话</button>
      ${primaryAction(l)}`;
    wireActions();
  }

  /** 绑定动作按钮（重画后需重新绑定，因此集中在这里）。 */
  function wireActions() {
    const back = document.getElementById("b-back");
    if (back) back.onclick = () => { location.hash = "#/courses"; };
    const speak = document.getElementById("b-speak");
    if (speak) speak.onclick = (e) => toggleSpeak(e.target);
    const pauseBtn = document.getElementById("b-pause");
    if (pauseBtn) pauseBtn.onclick = () => {
      S.voicePaused = !S.voicePaused;
      if (S.voicePaused) Voice.pause(); else Voice.resume();
      const b = document.getElementById("b-pause");
      if (b) b.textContent = S.voicePaused ? "▶ 继续" : "⏸ 暂停";
      renderStage();
    };
    const png = document.getElementById("b-png");
    if (png) png.onclick = exportBoardPng;
    const md = document.getElementById("b-md");
    if (md) md.onclick = exportBoardMd;
    const conv = document.getElementById("b-conv");
    if (conv) conv.onclick = exportConversation;

    const lb = document.getElementById("b-lecture");
    if (lb) lb.onclick = () => startLecture(lb);
    const lb2 = document.getElementById("b-lecture-2");
    if (lb2) lb2.onclick = () => startLecture(lb2);
    const pb = document.getElementById("b-practice");
    if (pb) pb.onclick = () => startPractice(pb);
    const db = document.getElementById("b-done");
    if (db) db.onclick = () => completeLesson(db);
  }

  /** 生成讲义（页签空状态按钮与顶栏按钮共用）。
   *
   * @param {HTMLButtonElement|null} btn
   * @param {{auto?:boolean}} opts  auto=true 表示由进入课堂自动触发（完成后弹窗）。
   */
  async function startLecture(btn, opts) {
    opts = opts || {};
    const l = S.lesson;
    // 「已完成」的讲次重新生成会**整体覆盖**既有讲义/讲稿/标注，而状态仍停在 done、
    // 界面上看不出区别（核验确认的静默覆盖）。只对这一种情形要用户确认；
    // 普通的「重新生成讲义」不加确认，免得每次都要多点一下。
    if (!opts.auto && l.board && l.status === "done"
      && !confirm("这一讲已标记为完成。重新生成会用新内容覆盖现有的讲义、讲稿与标注，继续吗？")) {
      return;
    }
    if (btn) btn.disabled = true;
    const box = document.getElementById("tab-body");
    try {
      const r = await Api.post("/api/courses/lessons/" + l.id + "/lecture");
      S.tab = "board";
      await pollJob(r.job_id, (stage) => {
        if (box) {
          box.innerHTML = `<div class="empty">正在为你准备这一讲…<div class="hint">${esc(stage || "")}</div><div class="bar"><i style="width:100%"></i></div></div>`;
        }
      });
      await loadLesson(l.id);
      // 讲义生成后按钮组合会变（生成讲义 → 标记完成），整块重画
      renderHead();
      renderTabs();
      renderTabBody();
      renderCitations();
      if (opts.auto) {
        showReadyModal();
      } else {
        Toast("讲义已生成");
      }
    } catch (e) {
      Toast(e.message, true);
      if (btn) btn.disabled = false;
      // 自动路径失败：回到可手动重试的空状态
      if (opts.auto && box) renderTabBody();
    }
  }

  /** 出题并进入练习页。 */
  async function startPractice(btn) {
    const l = S.lesson;
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "出题中…";
    try {
      if (!S.questionCount) {
        const r = await Api.post("/api/courses/lessons/" + l.id + "/practice", { count: 5 });
        await pollJob(r.job_id);
      }
      location.hash = "#/practice/" + l.id;
    } catch (e) {
      Toast(e.message, true);
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  /** 上课确认弹窗期间预热第一讲开头的几段：把「开播第一段合成」藏进用户
   *  读弹窗的时间里——流水线只保证段间不断流，开播第一段仍是冷启动，
   *  对慢的第三方语音 API 尤其有感。只预热前 360 字（约 4 段）控住云端成本。 */
  function primeFirstSlide() {
    try {
      const first = buildSlides()[0];
      if (first) {
        Voice.prime(scriptTextForSlide(first, 0), { chunkChars: SUB_CHARS, maxChars: 360 });
      }
    } catch (e) { /* 预热失败不影响上课 */ }
  }

  /* ── 上课确认弹窗 ─────────────────────── */
  /** 弹「准备好了吗？→ 开始上课」窗口；点「开始上课」后自动朗读讲义。 */
  function showReadyModal() {
    if (S.readyShown) return;
    S.readyShown = true;
    primeFirstSlide();
    const mask = el("div", "modal-mask");
    const box = el("div", "modal-box");
    box.innerHTML = `
      <div class="modal-title">准备好了吗？</div>
      <div class="hint">《${esc(S.lesson.title)}》的课件已经就绪。点击「开始上课」，我会按课件逐页讲解；
      屏幕中下方会同步显示字幕（不想听声音时可以直接读），讲完自动进入随堂测验。</div>`;
    const row = el("div", "row");
    row.style.marginTop = "16px";
    const go = el("button", "btn primary", "开始上课");
    const later = el("button", "btn", "稍后");
    go.onclick = () => { mask.remove(); startTeaching(); };
    later.onclick = () => mask.remove();
    row.appendChild(go); row.appendChild(later);
    box.appendChild(row);
    mask.appendChild(box);
    document.body.appendChild(mask);
  }

  /** 开始上课：切到「课件」页签，按页出现并逐页讲授。 */
  function startTeaching() {
    if (!S.lesson.board) return;
    S.slides = buildSlides();
    S.scripts = buildScripts();
    if (!S.slides.length) return Toast("这一节没有可讲的课件内容", true);
    S.slideIndex = -1;
    S.teaching = true;
    S.finished = false;
    S.voicePaused = false;
    S.speakLog = [];
    S.subtitle = "";
    S.tab = "slides";
    renderTabs();
    renderHead();      // 让「开始上课」按钮变成「停止」
    renderTabBody();
    renderSpeaking();
    renderStage();
    nextSlide();
  }

  /** 标记完成并跳到下一节未完成的讲次。 */
  async function completeLesson(btn) {
    const l = S.lesson;
    btn.disabled = true;
    try {
      await Api.put("/api/courses/lessons/" + l.id, { complete: true });
      const course = await Api.get("/api/courses/" + l.course_id);
      const rest = [];
      (course.units || []).forEach((u) => (u.lessons || []).forEach((x) => {
        if (x.id !== l.id) rest.push(x);
      }));
      const next = rest.find((x) => x.status !== "done");
      location.hash = next ? "#/lessons/" + next.id : "#/courses";
      if (!next) Toast("这门课已经学完了 🎉");
    } catch (e) {
      Toast(e.message, true);
      btn.disabled = false;
    }
  }

  async function render(host, lessonId) {
    // 回到同一讲：保留授课进度（切去别的页再回来不该重头讲）
    const resume = (S.lessonId === lessonId) && (S.teaching || S.finished);
    // 切到别的讲次或首次进入：停掉上一讲朗读，避免声音串台。
    if (!resume) Voice.stop();
    // 同步朗读引擎（系统语音 / 本地 MeloTTS），用户在设置页改过也能立刻生效。
    await Voice.syncFromServer();

    // 非授课状态：每次进入都重置（材料标注页、总结页依赖它们）
    S.tab = initialTab();
    S.marks = [];
    S.unitSummary = null;
    S.docIndex = 0;
    S.pageNo = 1;
    S.scale = 1.2;
    S.mode = "view";
    S.readyShown = false;
    S.enteredWithTab = /\btab=/.test((location.hash.split("?")[1] || ""));

    // 授课相关状态：仅在非 resume 时重置（resume 时保留，支持切页续讲）
    if (!resume) {
      S.teaching = false;
      S.finished = false;
      S.voicePaused = false;
      S.speakLog = [];
      S.speaking = "";
      S.subtitle = "";
      S.slides = [];
      S.scripts = [];
      S.slideIndex = 0;
    }

    await loadLesson(lessonId);
    S.lessonId = lessonId;
    // 上面算 S.tab 时 S.lesson 还没加载（或还是上一讲），所以这里要按**真实讲次类型**
    // 再校正一次：否则练习讲次的 S.tab 会停在 "slides" 这种本讲次并不存在的值上——
    // 表现为页签一个都不高亮，正文还错落到「单元总结」。
    if (!visibleTabs().some(([k]) => k === S.tab)) S.tab = visibleTabs()[0][0];
    await ensureConversation();

    host.innerHTML = `
      <div class="cols">
        <div class="col col-origin" id="lesson-origin"></div>
        <div class="col col-main" style="display:flex;flex-direction:column;min-width:0">
          <div class="lesson-head" id="lesson-head"></div>
        </div>
        <div class="col col-right" style="width:340px;display:flex;flex-direction:column">
          <div id="lesson-speaking" style="flex:0 0 auto;display:flex;flex-direction:column;border-bottom:1px solid var(--border)"></div>
          <div id="lesson-cites" style="flex:0 0 auto;display:flex;flex-direction:column;min-height:0;border-bottom:1px solid var(--border)"></div>
          <div style="flex:1 1 60%;display:flex;flex-direction:column;min-height:0">
            <div class="panel-head"><span>课堂提问</span></div>
            <div class="chat-scroll" id="lesson-chat"></div>
            <div class="chat-input">
              <div class="box">
                <button class="btn" id="b-mic" title="本地语音识别：点一下开始录音，再点一下转成文字">🎙 语音</button>
                <textarea id="lesson-input" placeholder="针对这一节提问，Enter 发送；也可以点🎙直接说"></textarea>
                <button class="btn primary" id="b-send">发送</button>
              </div>
            </div>
          </div>
        </div>
      </div>
      <!-- 授课舞台：屏幕中下方常驻字幕 + 暂停时的互动选择（固定定位，不随页面滚动） -->
      <div class="teach-stage" id="teach-stage" hidden>
        <div class="teach-sub" id="teach-sub"></div>
      </div>`;

    // 讲义里的引用角标 → 左栏原件翻到对应页。
    // 委托绑在本次新建的 .cols 上：切页会重建 → 监听自动失效，不会累积。
    const colsEl = host.querySelector(".cols");
    if (colsEl) {
      colsEl.addEventListener("click", (e) => {
        const btn = e.target.closest(".cite");
        if (btn && btn.dataset.cite) jumpOriginToCite(btn.dataset.cite);
      });
    }

    renderHead();
    renderStage();
    renderOrigin();
    document.getElementById("b-send").onclick = send;
    document.getElementById("lesson-input").onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    };

    // 语音输入：复用公共模块（工作台同一个实现），音频只送本机 ASR，不外发。
    const mic = document.getElementById("b-mic");
    if (mic) {
      mic.onclick = () => window.Asr && Asr.toggle(
        mic, () => document.getElementById("lesson-input"),
        { idleText: "🎙 语音", toast: (m, bad) => Toast(m, bad) });
    }

    renderTabs();
    renderTabBody();
    renderCitations();
    renderSpeaking();
    renderChat();

    // 浮动「回到课堂」入口：根据当前授课状态显隐
    ensurePillListener();
    updateReturnPill();

    if (resume) {
      // 恢复界面（renderTabBody 会按 S.teaching 只渲染到当前页）。
      // 若离开期间朗读断了（且未主动暂停），从当前页继续讲；若仍在朗读则
      // 字幕会随 onChunk 继续刷新，什么都不做。
      if (S.teaching && !S.voicePaused && !Voice.isSpeaking()) {
        const idx = Math.min(Math.max(S.slideIndex, 0), (S.slides || []).length - 1);
        if (S.slides[idx]) speakSlide(S.slides[idx]);
      }
    } else {
      // 自动上课流：讲次没有讲义 → 自动生成；已有讲义 → 弹「准备好了吗」
      if (S.lesson.kind !== "practice") {
        if (!S.lesson.board) {
          startLecture(null, { auto: true });
        } else if (!S.enteredWithTab) {
          showReadyModal();
        }
      }
    }
  }

  /* ── 回到课堂浮动入口 ─────────────────── */
  // 上课期间切到别的页面时，在 document.body 上挂一个固定按钮，点它回到课堂。
  // 不放进 #view，避免被别的视图重绘清掉；用模块级布尔防止重复注册监听。
  let _returnPill = null;
  let _pillRegistered = false;

  /** 根据当前授课状态显隐浮动入口（不在课堂页且正在上课时出现）。 */
  function updateReturnPill() {
    if (!_returnPill) {
      _returnPill = el("button", "lesson-return-pill");
      _returnPill.id = "lesson-return";
      _returnPill.hidden = true;
      _returnPill.onclick = () => { if (S.lessonId) location.hash = "#/lessons/" + S.lessonId; };
      document.body.appendChild(_returnPill);
    }
    const show = !!(S.lessonId && (S.teaching || S.finished)
      && !location.hash.startsWith("#/lessons/" + S.lessonId));
    if (show) {
      _returnPill.textContent = "● 正在上《" + (S.lesson ? S.lesson.title : "") + "》 · 回到课堂";
      _returnPill.hidden = false;
    } else {
      _returnPill.hidden = true;
    }
  }

  /** 一次性注册 hashchange 监听（模块级布尔防重复）。 */
  function ensurePillListener() {
    if (_pillRegistered) return;
    _pillRegistered = true;
    window.addEventListener("hashchange", updateReturnPill);
  }

  // 仅供测试：只读探针，便于交互级验证读取课堂内部状态
  window.LessonProbe = () => ({
    teaching: S.teaching, finished: S.finished,
    slideIndex: S.slideIndex, voicePaused: S.voicePaused, lessonId: S.lessonId,
  });

  // 仅供测试：跳到指定页并重走课件渲染（含真实的自动滚动定位逻辑）。
  // CDP 里无法等待真实朗读结束再翻页，用它验证「新页是否被字幕条/操作条遮挡」。
  window.__lessonScrollTo = (idx) => {
    if (!S.teaching || !S.slides.length) return false;
    const i = Math.max(0, Math.min(Number(idx) || 0, S.slides.length - 1));
    S.slideIndex = i;
    S.tab = "slides";
    renderTabs();
    renderTabBody();      // 清空并重绘，内含真实的自动滚动定位
    return true;
  };

  window.Views = window.Views || {};
  window.Views.lesson = { render };
})();
