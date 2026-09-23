/* 资料生成页：速查表 / 笔记 / 思维导图 / Quiz / 闪卡 + 导出。 */
(function () {
  "use strict";

  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const TYPES = [
    ["cheatsheet", "速查表", "把整学期课件压缩成一页可翻阅的重点"],
    ["notes", "学习笔记", "核心概念 / 简记口诀 / 易错点 / 章节框架"],
    ["mindmap", "思维导图", "建立知识框架与逻辑关系"],
    ["quiz", "练习题", "当场做题当场纠错（含解析）"],
    ["flashcard", "闪卡", "问答式主动回忆，可导出 Anki"],
  ];
  const S = { type: "cheatsheet", docs: [], selected: [], lengths: "standard", count: 10, current: null, polls: {} };

  async function loadDocs() {
    const d = await Api.get("/api/documents?page=1&page_size=200");
    S.docs = (d.items || []).filter((x) => x.status === "ready");
  }

  async function render(host) {
    await loadDocs();
    host.innerHTML = "";
    const page = document.createElement("div");
    page.className = "page";
    page.innerHTML = `
      <div class="card">
        <div class="field"><label>来源材料（${S.docs.length} 份可选）</label><div id="gen-docs" class="row" style="flex-wrap:wrap"></div></div>
        <div class="tabs" id="gen-tabs"></div>
        <div class="row" style="align-items:flex-end">
          <div class="field"><label>篇幅</label>
            <select id="gen-length">
              <option value="brief">精简</option><option value="standard" selected>标准</option><option value="detailed">详尽</option>
            </select></div>
          <div class="field" id="gen-count-wrap"><label>数量</label><input type="text" id="gen-count" value="10"></div>
          <div style="flex:1"></div>
          <button class="btn primary" id="gen-go">开始生成</button>
        </div>
      </div>
      <div id="gen-preview"></div>`;
    host.appendChild(page);

    renderTabs();
    renderDocs();
    updateCountVis();
    document.getElementById("gen-length").onchange = (e) => { S.lengths = e.target.value; };
    document.getElementById("gen-count").onchange = (e) => {
      const n = parseInt(e.target.value || "10", 10);
      S.count = Number.isFinite(n) ? n : 10;
    };
    document.getElementById("gen-go").onclick = start;
    showPreview(null);
  }

  function renderTabs() {
    const box = document.getElementById("gen-tabs");
    box.innerHTML = "";
    TYPES.forEach(([id, name, tip]) => {
      const b = document.createElement("button");
      b.textContent = name;
      b.title = tip;
      if (id === S.type) b.classList.add("active");
      b.onclick = () => { S.type = id; renderTabs(); updateCountVis(); };
      box.appendChild(b);
    });
  }
  function updateCountVis() {
    document.getElementById("gen-count-wrap").style.display =
      (S.type === "quiz" || S.type === "flashcard") ? "" : "none";
  }

  function renderDocs() {
    const box = document.getElementById("gen-docs");
    box.innerHTML = "";
    if (!S.docs.length) { box.innerHTML = '<span class="hint">还没有已解析完成的材料，请先到「工作台」上传。</span>'; return; }
    S.docs.forEach((d) => {
      const lab = document.createElement("label");
      lab.className = "pill" + (S.selected.includes(d.id) ? " ok" : "");
      lab.style.cursor = "pointer";
      lab.innerHTML = `<input type="checkbox" style="margin-right:5px" ${S.selected.includes(d.id) ? "checked" : ""}>${d.title}`;
      lab.querySelector("input").onchange = (e) => {
        if (e.target.checked) S.selected.push(d.id);
        else S.selected = S.selected.filter((x) => x !== d.id);
        renderDocs();
      };
      box.appendChild(lab);
    });
  }

  async function start() {
    if (!S.selected.length) return Toast("请先选择至少 1 份来源材料", true);
    try {
      const d = await Api.post("/api/generations", {
        type: S.type, document_ids: S.selected,
        params: { length: S.lengths, count: S.count, language: "zh-CN" },
      });
      Toast("生成中…");
      showPreview({ id: d.generation_id, status: "running" });
      poll(d.generation_id);
    } catch (e) { Toast(e.message, true); }
  }

  async function poll(id) {
    for (let i = 0; i < 300; i++) {
      const g = await Api.get("/api/generations/" + id);
      if (g.status === "ready") { S.current = g; showPreview(g); Toast("生成完成"); return; }
      if (g.status === "failed") { showPreview(g); Toast("生成失败：" + g.error, true); return; }
      await new Promise((r) => setTimeout(r, 700));
    }
    Toast("生成超时", true);
  }

  function showPreview(g) {
    const box = document.getElementById("gen-preview");
    box.innerHTML = "";
    if (!g) { box.innerHTML = '<div class="empty">选择材料与类型后点击「开始生成」</div>'; return; }
    const card = document.createElement("div");
    card.className = "card";
    if (g.status === "running") {
      card.innerHTML = `<div class="empty">⏳ 生成中，请稍候…</div>`;
      box.appendChild(card); return;
    }
    if (g.status === "failed") {
      card.innerHTML = `<div class="empty">生成失败：${g.error || ""}</div>`;
      box.appendChild(card); return;
    }

    const head = document.createElement("div");
    head.className = "row";
    head.style.marginBottom = "10px";
    head.innerHTML = `<b>${g.title || ""}</b><span style="flex:1"></span>`;
    const dl = document.createElement("button");
    dl.className = "btn small";
    dl.textContent = "导出 ▾";
    dl.onclick = () => exportMenu(g, dl);
    head.appendChild(dl);
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "preview";
    if (S.current && S.current.type === "mindmap") {
      const mm = document.createElement("div");
      body.appendChild(mm);
      card.appendChild(body);
      const svg = MD.mindmap(mm, g.content_md || "");
      if (svg) {
        const png = document.createElement("button");
        png.className = "btn small"; png.textContent = "导出图片";
        png.style.marginLeft = "8px";
        png.onclick = () => exportImage(g, svg);
        head.appendChild(png);
      }
    } else if (g.type === "quiz") {
      (g.content_json.items || []).forEach((it, i) => {
        const q = document.createElement("div");
        q.className = "quiz-item";
      q.innerHTML = `<b>${esc(i + 1)}. ${esc(it.stem || "")}</b>` +
        (it.options || []).map((o, j) => `<span class="opt ${j === it.answer_index ? "correct" : ""}">${esc("ABCD"[j] || (j + 1))}. ${esc(o || "")}${j === it.answer_index ? " ✓" : ""}</span>`).join("") +
        `<div class="hint">解析：${esc(it.explanation || "—")}</div>`;
        body.appendChild(q);
      });
      card.appendChild(body);
    } else if (g.type === "flashcard") {
      (g.content_json.items || []).forEach((it) => {
        const c = document.createElement("div");
        c.className = "flashcard";
        c.textContent = it.question;
        c.onclick = () => {
          c.classList.toggle("flip");
          c.textContent = c.classList.contains("flip") ? it.answer : it.question;
        };
        body.appendChild(c);
        body.appendChild(document.createElement("div")).style.height = "10px";
      });
      card.appendChild(body);
    } else {
      MD.mount(body, g.content_md || "");
      card.appendChild(body);
    }
    box.appendChild(card);
  }

  function exportMenu(g, btn) {
    const opts = g.type === "mindmap"
      ? [["markdown", "Markdown"]]
      : g.type === "quiz" || g.type === "flashcard"
        ? [["anki", "Anki (.apkg)"], ["markdown", "Markdown"], ["csv", "CSV"]]
        : [["markdown", "Markdown"]];
    const old = document.getElementById("export-pop");
    if (old) old.remove();
    const pop = document.createElement("div");
    pop.id = "export-pop";
    pop.style.cssText = "position:absolute;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:var(--shadow);padding:6px;z-index:10";
    opts.forEach(([fmt, name]) => {
      const b = document.createElement("button");
      b.className = "btn small"; b.style.cssText = "display:block;width:100%;text-align:left;border:0";
      b.textContent = name;
      b.onclick = async () => {
        pop.remove();
        try {
          const r = await Api.post("/api/exports", { generation_id: g.id, format: fmt });
          Api.download(r.download_url);
          Toast("已导出 " + r.file_name);
        } catch (e) { Toast(e.message, true); }
      };
      pop.appendChild(b);
    });
    const rect = btn.getBoundingClientRect();
    pop.style.left = rect.left + "px";
    pop.style.top = rect.bottom + 4 + "px";
    document.body.appendChild(pop);
    setTimeout(() => document.addEventListener("click", () => pop.remove(), { once: true }), 0);
  }

  async function exportImage(g, svg) {
    const xml = new XMLSerializer().serializeToString(svg);
    const img = new Image();
    const blob = new Blob([xml], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    img.onload = async () => {
      const cv = document.createElement("canvas");
      cv.width = 1600; cv.height = 900;
      const ctx = cv.getContext("2d");
      ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.drawImage(img, 0, 0, cv.width, cv.height);
      URL.revokeObjectURL(url);
      cv.toBlob(async (b) => {
        try {
          const fd = new FormData();
          fd.append("image", b, "mindmap.png");
          const r = await Api.upload(`/api/exports/image?generation_id=${g.id}`, fd);
          Api.download(r.download_url);
          Toast("已导出图片");
        } catch (e) { Toast(e.message, true); }
      }, "image/png");
    };
    img.src = url;
  }

  window.Views = window.Views || {};
  window.Views.generate = { render };
})();
