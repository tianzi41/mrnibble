/* 记忆管理页：查看 / 编辑 / 删除（硬删除）/ 导出。 */
(function () {
  "use strict";

  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const S = { items: [], total: 0, page: 1, type: "", editing: null };
  const TYPE_NAMES = { preference: "偏好", progress: "进度", knowledge_gap: "知识盲区", fact: "事实" };

  async function render(host) {
    host.innerHTML = "";
    const page = document.createElement("div");
    page.className = "page";
    page.innerHTML = `
      <div class="row" style="margin-bottom:14px">
        <div class="field" style="max-width:200px"><label>类型</label>
          <select id="mm-type">
            <option value="">全部</option>
            <option value="preference">偏好</option><option value="progress">进度</option>
            <option value="knowledge_gap">知识盲区</option><option value="fact">事实</option>
          </select></div>
        <span style="flex:1"></span>
        <button class="btn" id="mm-add">＋ 新增</button>
        <button class="btn" id="mm-export">导出</button>
        <button class="btn danger" id="mm-clear">清空</button>
      </div>
      <div id="mm-list"></div>
      <div class="row" style="margin-top:12px;justify-content:center" id="mm-pager"></div>
      <p class="hint" style="text-align:center">删除是物理删除：条目会同时从检索索引与向量库中移除，之后不会再被召回。</p>`;
    host.appendChild(page);

    document.getElementById("mm-type").onchange = (e) => { S.type = e.target.value; S.page = 1; load(); };
    document.getElementById("mm-add").onclick = add;
    document.getElementById("mm-export").onclick = () => Api.download("/api/memories/export?format=md");
    document.getElementById("mm-clear").onclick = async () => {
      if (!confirm("确认清空全部记忆？此操作不可恢复。")) return;
      await Api.del("/api/memories"); Toast("已清空"); load();
    };
    await load();
  }

  async function load() {
    const q = new URLSearchParams({ page: S.page, page_size: 50 });
    if (S.type) q.set("type", S.type);
    const d = await Api.get("/api/memories?" + q);
    S.items = d.items || []; S.total = d.total || 0;
    paint();
  }

  function paint() {
    const box = document.getElementById("mm-list");
    box.innerHTML = "";
    if (!S.items.length) { box.innerHTML = '<div class="empty">还没有记忆</div>'; return; }
    const tbl = document.createElement("table");
    tbl.className = "tbl";
    tbl.innerHTML = `<thead><tr><th style="width:88px">类型</th><th>内容</th><th style="width:90px">召回</th><th style="width:170px">更新时间</th><th style="width:150px">操作</th></tr></thead>`;
    const tb = document.createElement("tbody");
    S.items.forEach((m) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><span class="pill ${m.type === "knowledge_gap" ? "warn" : ""}">${TYPE_NAMES[m.type] || m.type}</span></td>
        <td data-content>${esc(m.content)}</td>
        <td>${m.recall_count} 次</td>
        <td class="hint">${(m.updated_at || "").replace("T", " ").slice(0, 16)}</td>`;
      const ops = document.createElement("td");
      const edit = document.createElement("button");
      edit.className = "btn small"; edit.textContent = "编辑";
      edit.onclick = () => editItem(m, tr);
      const del = document.createElement("button");
      del.className = "btn small danger"; del.textContent = "删除"; del.style.marginLeft = "6px";
      del.onclick = async () => {
        if (!confirm("删除这条记忆？删除后不会再被召回。")) return;
        await Api.del("/api/memories/" + m.id); Toast("已删除"); load();
      };
      ops.appendChild(edit); ops.appendChild(del);
      tr.appendChild(ops);
      tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    box.appendChild(tbl);
    renderPager();
  }

  function editItem(m, tr) {
    const cell = tr.querySelector("[data-content]");
    const input = document.createElement("input");
    input.type = "text"; input.value = m.content; input.style.width = "100%";
    cell.innerHTML = ""; cell.appendChild(input);
    input.focus();
    input.onkeydown = async (e) => {
      if (e.key === "Enter") {
        await Api.patch("/api/memories/" + m.id, { content: input.value });
        Toast("已更新"); load();
      } else if (e.key === "Escape") load();
    };
  }

  function add() {
    const content = prompt("记忆内容（如：讲解时希望配具体例证）：");
    if (!content || !content.trim()) return;
    const type = prompt("类型：preference / progress / knowledge_gap / fact", "preference") || "preference";
    Api.post("/api/memories", { type, content: content.trim() })
      .then(() => { Toast("已添加"); load(); })
      .catch((e) => Toast(e.message, true));
  }

  function renderPager() {
    const box = document.getElementById("mm-pager");
    box.innerHTML = "";
    const pages = Math.max(1, Math.ceil(S.total / 50));
    if (pages <= 1) return;
    for (let i = 1; i <= pages; i++) {
      const b = document.createElement("button");
      b.className = "btn small" + (i === S.page ? " primary" : "");
      b.textContent = i;
      b.onclick = () => { S.page = i; load(); };
      box.appendChild(b);
    }
  }

  window.Views = window.Views || {};
  window.Views.memory = { render };
})();
