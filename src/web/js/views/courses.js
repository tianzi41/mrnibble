/* 课程：列表 + 创建向导（目标/基础/体量 → 大纲生成 → 结构确认）。 */
(function () {
  "use strict";

  const S = {
    courses: [],
    documents: [],
    courseId: null,
    course: null,       // 当前查看的课程详情
    creating: false,
    // 从课程页的「AI 材料」入口点进来时要接回的任务（一次性，用完即清）。
    // ⚠️ 它必须活过整页重渲染：点任务会改 hash → hashchange → 整个视图重建。
    pendingMaterial: null,
    // 课程页要展示的材料任务（type='material'），只留「还没收尾」的。
    materialJobs: [],
    mindmap: null,      // 结构预览的 markmap 实例
  };

  const LEVELS = [["beginner", "零基础"], ["intermediate", "有基础"], ["advanced", "进阶"]];
  const DEPTHS = [["brief", "概览（快速过一遍）"], ["standard", "标准"], ["detailed", "深入（含推导与易错点）"]];

  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  };
    const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* ── 新建课程草稿（本地只存「最新一次未完成的创建」）─────────────
   *
   * 用户实测问题（2026-09-19）：创建到一半切到别的页再回来，向导被重建、进度全丢。
   * 课程页原来唯一的「回来的路」是「AI 材料」区块，而它只认**后端材料任务** ——
   * 用「我上传的材料」建课时根本没有材料任务，于是那块只剩一条很早以前、
   * 永远不会收尾的旧记录（既不建课也不再更新），用户点到的是「上上次的进度」。
   *
   * 修法：向导里任何变化都写进**单键** localStorage（单键 = 最新天然覆盖更早，
   * 不会越攒越多）；再进向导时自动填回，并在课程页给一条「未完成的创建」入口置顶。
   * 只有三件事会清掉草稿：点「取消」、点「清空草稿，重新开始」、建课成功。
   */
  const DRAFT_KEY = "zhiban-create-draft";
  const DRAFT_VER = 2;
  let draftCollector = null;   // 向导渲染时把自己的「收集函数」挂进来
  let draftTimer = null;

  function saveDraft(o) {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({ v: DRAFT_VER, at: Date.now(), ...o }));
    } catch (e) { /* 隐私模式等，忽略 */ }
  }
  function loadDraft() {
    try {
      const raw = localStorage.getItem(DRAFT_KEY);
      if (!raw) return null;
      const d = JSON.parse(raw);
      return (d && d.v === DRAFT_VER) ? d : null;
    } catch (e) { return null; }
  }
  function clearDraft() {
    try { localStorage.removeItem(DRAFT_KEY); } catch (e) { /* 忽略 */ }
  }
  /** 草稿里有没有**实质内容**：全是默认值就不留草稿，免得每次进向导都弹一条「已恢复」。 */
  function draftMeaningful(d) {
    if (!d) return false;
    const t = d.topic || {};
    return !!(d.intentPrimary || String(d.goal || "").trim()
      || (d.docIds || []).length || t.topic || t.gid || t.docId
      || String(d.note || "").trim());
  }
  function draftTitle(d) {
    const t = d.topic || {};
    const s = t.topic || String(d.goal || "").split(/[。！？\n]/)[0] || "新建课程";
    return s.length > 22 ? s.slice(0, 22) + "…" : s;
  }
  function draftTimeText(at) {
    if (!at) return "";
    const d = new Date(at);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  /** 延后保存（输入时连续触发，300ms 合并一次）。
   *  写成函数声明是为了让向导内部的 paintSrc / paintChapters 等**任意位置都能安全调用**
   *  （箭头函数的 TDZ 会让「先调用后定义」直接报错）。 */
  function scheduleDraftSave() {
    if (draftTimer) clearTimeout(draftTimer);
    draftTimer = setTimeout(saveDraftNow, 300);
  }
  function saveDraftNow() {
    if (!draftCollector) return;                 // 不在向导里（如结构编辑）→ 不动草稿
    let d = null;
    // 视图可能已被路由替换（向导 DOM 已不在页面上）→ 收集函数会读不到元素。
    // 这种情况直接把这个失效的收集函数丢掉，别让定时器反复抛错。
    try { d = draftCollector(); } catch (e) { draftCollector = null; return; }
    if (!draftMeaningful(d)) { clearDraft(); return; }
    saveDraft(d);
  }

  /* ── 数据 ─────────────────────────────── */
  async function loadCourses() {
    const d = await Api.get("/api/courses");
    S.courses = d.items || [];
  }
  async function loadDocuments() {
    try {
      const d = await Api.get("/api/documents?page=1&page_size=200");
      S.documents = (d.items || []).filter((x) => x.status === "ready");
    } catch (e) { S.documents = []; }
  }
  async function loadCourse(id) {
    S.courseId = id;
    S.course = await Api.get("/api/courses/" + id);
  }

  /* ── 左侧课程列表 ─────────────────────── */
  function renderList() {
    const box = document.getElementById("course-list");
    box.innerHTML = "";
    box.appendChild(el("div", "panel-head",
      `<span>课程（${S.courses.length}）</span><button class="btn small primary" id="btn-new">＋ 新建课程</button>`));
    const body = el("div", "panel-body");
    if (!S.courses.length) {
      body.appendChild(el("div", "empty", "还没有课程。<br>点「＋ 新建课程」，选材料、写下学习目标即可生成。"));
    }
    S.courses.forEach((c) => {
      const p = c.progress || {};
      const item = el("div", "item" + (c.id === S.courseId ? " active" : ""));
      item.appendChild(el("span", "t", esc(c.title)));
      item.appendChild(el("small", null, `${p.done_lessons || 0}/${p.total_lessons || 0}`));
      const del = el("button", "x", "✕");
      del.title = "删除课程";
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除课程《${c.title}》？单元、讲次与练习记录会一并删除。`)) return;
        await Api.del("/api/courses/" + c.id);
        if (S.courseId === c.id) { S.courseId = null; S.course = null; }
        await loadCourses(); renderList(); renderMain();
      };
      item.appendChild(del);
      // ⚠️ 必须走 openCourse：它会清掉 S.creating。
      // 只调 renderMain() 的话，向导状态还在 → renderMain 又把向导顶出来 →
      // 用户点左侧任何课程都进不去，看起来就是「锁死在新建课程界面」（2026-09-17 实测）。
      item.onclick = () => openCourse(c.id);
      body.appendChild(item);
    });
    box.appendChild(body);
    document.getElementById("btn-new").onclick = openCreate;
    // 「未完成的创建」入口：本地草稿永远只存**最新那一次**，所以它天然排在最前、
    // 压过下面「AI 材料」里的旧记录（用户实测：以前这里只剩一条很早的旧任务，
    // 点进去是上上次的进度，本次创建的进度反而找不回来）。
    const d = loadDraft();
    if (draftMeaningful(d)) {
      const panel = el("div");
      panel.style.marginTop = "10px";
      panel.appendChild(el("div", "panel-head", "<span>未完成的创建</span>"));
      const pbody = el("div", "panel-body");
      const it = el("div", "item");
      it.title = "点击回到新建向导，继续上次未完成的创建";
      it.appendChild(el("span", "t", esc(draftTitle(d))));
      it.appendChild(el("small", null, draftTimeText(d.at)));
      it.onclick = () => openCreate();
      pbody.appendChild(it);
      panel.appendChild(pbody);
      box.appendChild(panel);
    }
    renderMaterialJobs(box);
  }

  /** 课程列表下方的「AI 材料」区块：只显示**还没收尾**的材料生成任务。
   *
   *  为什么需要它：材料是逐章写的、要跑几十秒，用户中途切到别的页是常态。
   *  此前进度只活在新建向导的内存里，向导一重建就再也找不回来
   *  （用户实测：「写正文到一半切到别的页再回来，我找不回来了」）。
   *  这里改成**从后端任务行恢复**：`GET /api/generations?type=material` 的任务里有
   *  目录 JSON 和「已写了几章」，所以切页、甚至关掉软件重开都能接上。
   */
  function renderMaterialJobs(box) {
    // 最新在前（后端已 ORDER BY created_at DESC，这里再兜一次底，防以后改排序）
    const jobs = (S.materialJobs || []).filter(materialJobVisible)
      .slice()
      .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    if (!jobs.length) return;
    const panel = el("div");
    panel.style.marginTop = "10px";
    panel.appendChild(el("div", "panel-head", `<span>AI 材料（${jobs.length}）</span>`));
    const body = el("div", "panel-body");
    const row = (j) => {
      const it = el("div", "item");
      it.title = "点击继续 / 用它建课";
      it.appendChild(el("span", "t", esc(materialJobTitle(j))));
      it.appendChild(el("small", null, materialJobBadge(j)));
      it.onclick = () => { S.pendingMaterial = j; openCreate(); };
      return it;
    };
    // 只展开**最新一条**：更早的多半是早就不再收尾的旧任务，全摊开会把最新的那条埋掉
    // （用户实测：这里只剩一条很早的旧记录，点进去是上上次的进度）。
    body.appendChild(row(jobs[0]));
    if (jobs.length > 1) {
      const n = jobs.length - 1;
      const more = el("button", "btn small", `更早的 ${n} 条 ▾`);
      more.type = "button";
      more.style.marginTop = "6px";
      let open = false;
      more.onclick = () => {
        open = !open;
        more.textContent = open ? `收起更早的 ${n} 条 ▴` : `更早的 ${n} 条 ▾`;
        Array.from(body.querySelectorAll(".item.older")).forEach((x) => x.remove());
        if (open) {
          jobs.slice(1).forEach((j) => {
            const it = row(j);
            it.classList.add("older");
            body.insertBefore(it, more);
          });
        }
      };
      body.appendChild(more);
    }
    panel.appendChild(body);
    box.appendChild(panel);
  }

  /** 材料任务该不该出现在课程页：
   *  · 进行中 / 已中断 → 显示（用户需要一条「回来的路」）
   *  · 写完但**还没有任何课程用它** → 显示（等着建课）
   *  · 写完且已被某门课引用 → 不显示（这条链路已经走完了） */
  function materialJobVisible(j) {
    if (j.status === "running" || j.status === "failed") return true;
    const did = (j.content_json || {}).doc_id || "";
    if (!did) return false;
    return !(S.courses || []).some((c) => (c.document_ids || []).includes(did));
  }
  function materialJobTitle(j) {
    return (((j.content_json || {}).outline) || {}).title || j.title || "AI 材料";
  }
  function materialJobBadge(j) {
    if (j.status === "running") {
      const n = ((j.content_md || "").match(/^## /gm) || []).length;
      const total = (((((j.content_json || {}).outline) || {}).chapters) || []).length;
      return total ? `生成中 ${n}/${total}` : "生成中";
    }
    if (j.status === "failed") return "已中断";
    return "待建课";
  }
  /** 拉一次材料任务列表（课程页渲染时调用）。 */
  async function loadMaterialJobs() {
    try {
      const d = await Api.get("/api/generations?type=material");
      S.materialJobs = (d.items || []).slice(0, 20);
    } catch (e) { S.materialJobs = []; }
  }

  /* ── 视图入口（hash 是「是否在新建向导」的唯一真相源）─────
     S.creating 曾经有两个真相源：点击时改状态 + render() 里按 hash 覆盖。
     而「＋新建课程」只改了状态、没改 hash，于是同一个视图内再点课程/大纲时
     renderMain 会一直把向导顶回前面 —— 用户看到的就是「锁死在新建课程界面」
     （2026-09-17 实测）。现在统一走下面三个入口，状态与 hash 永远一致。 */

  /** 改 hash，返回「是否已交给路由重渲染」。
   *  hash 相同时浏览器**不会**派发 hashchange → 那种情况要调用方自己重渲染。 */
  function goHash(h) {
    if (location.hash === h) return false;
    location.hash = h;
    return true;
  }
  /** 进入新建向导。 */
  function openCreate() {
    S.creating = true; S.course = null;
    if (!goHash("#/courses?new=1")) { renderList(); renderMain(); }
  }
  /** 离开向导（取消、或进课程详情时都要清，否则向导会一直霸占主区）。
   *  点「取消」= 用户明确放弃这次创建 → 连本地草稿一起清掉；
   *  （切页/进课程详情**不能**清草稿，那正是要恢复的场景。） */
  function closeCreate() {
    S.creating = false; S.pendingMaterial = null;
    draftCollector = null;
    clearDraft();
    if (!goHash("#/courses")) { renderList(); renderMain(); }
  }
  /** 打开某门课程详情。 */
  async function openCourse(id) {
    S.creating = false; S.pendingMaterial = null;
    draftCollector = null;          // 向导已卸载 → 别再往草稿里写（DOM 已不在）
    // ⚠️ 必须**先**把 courseId 设上再改 hash：改 hash 触发的是 route() → render()，
    // 而 render() 是靠 `S.courseId` 去加载详情的 —— 顺序反了就会加载「上一门」课程。
    S.courseId = id; S.course = null;
    if (goHash("#/courses")) return;      // 已改 hash → 交给 route() 渲染
    try { await loadCourse(id); } catch (e) { Toast(e.message, true); }
    renderList(); renderMain();
  }

  /* ── 主区分发 ─────────────────────────── */
  function renderMain() {
    const host = document.getElementById("course-main");
    // 切页/异步回调可能落在视图已被替换之后 → host 为 null（原来会直接抛错）
    if (!host) return;
    host.innerHTML = "";
    if (S.creating) return renderCreate(host);
    if (!S.course) {
      host.appendChild(el("div", "empty",
        "从左侧选择一门课程，或新建一门。<br><br>课程会把你的材料拆成「单元 → 讲次」，" +
        "每节讲次有白板讲义，学完可以做随堂练习。"));
      return;
    }
    renderDetail(host);
  }

  /* ── 新建课程（创建向导）───────────────── */
  function renderCreate(host) {
    // 本地保存的草稿 = **最新一次未完成的创建**（单键覆盖，不会攒旧记录）。
    // DOM 建好后再填回去（见下面 applyDraft），课型卡要等课型库拉回来才能选中。
    const draft = loadDraft();
    // 每次进向导都从**干净状态**开始：S.topicMode 是模块级单例，不重置的话
    // 上一次的「✅ 材料已就绪：《…》」会残留到下一次新建课程
    // （用户实测：「回来重新点新建课程，发现有上次创建课程的残留」）。
    // 真正需要「接着上次」的任务，改由课程页的「AI 材料」入口按后端状态恢复。
    S.topicMode = null;
    const card = el("div", "card");
    card.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b>新建课程</b>
        <span class="pill">第 1 步 / 共 2 步：选课型</span>
      </div>
      <div class="sep"></div>
      <div class="field">
        <label>学习材料</label>
        <div class="row" style="gap:6px">
          <button class="btn small src-tab" id="src-docs" type="button">用我上传的材料</button>
          <button class="btn small src-tab" id="src-topic" type="button">没有材料，我直接说想学什么</button>
        </div>
        <div id="pane-docs" style="margin-top:8px">
          <div id="pick-docs" class="doc-pick"></div>
          <div class="hint">不选 = 使用全部已解析材料</div>
        </div>
        <div id="pane-topic" style="display:none;margin-top:8px">
          <div class="row" style="gap:8px;align-items:flex-end">
            <div class="field" style="margin:0">
              <label>想学什么？（一句话主题）</label>
              <input type="text" id="f-topic" placeholder="例如：Python 装饰器 / 宏观经济学入门">
            </div>
            <div class="field" style="margin:0;max-width:170px">
              <label>材料篇幅</label>
              <select id="f-topic-depth">
                <option value="brief">精简（4 章）</option>
                <option value="standard" selected>标准（6 章）</option>
                <option value="detailed">详细（8 章）</option>
              </select>
            </div>
            <button class="btn small" id="t-outline" type="button">① 生成目录</button>
          </div>
          <div class="hint" id="t-hint">AI 会先写出一份教学材料并入库，再基于它备课 —— 这样讲义的引用页码仍然真实可查。</div>
          <div id="t-outline-wrap" style="display:none;margin-top:8px">
            <div class="hint">目录（可直接改标题、删掉不需要的章；改完再写正文）</div>
            <div id="t-chapters"></div>
            <div class="row" style="margin-top:6px">
              <button class="btn small" id="t-add" type="button">＋ 加一章</button>
              <button class="btn small primary" id="t-write" type="button">② 确认目录，开始写正文</button>
            </div>
          </div>
        </div>
      </div>
      <div class="field">
        <label>你想要什么课？<span class="hint" style="margin-left:6px">（决定单元怎么分、按什么顺序讲）</span>
          <button class="btn small" id="f-analyze" style="margin-left:8px">✨ 分析材料并预选</button>
        </label>
        <div id="intent-cards" class="intent-cards"></div>
        <div id="intent-hint" class="hint" style="margin-top:6px"></div>
      </div>
      <div class="field">
        <label>学习目标（一句话；AI 按课型写，可改、可清空）</label>
        <textarea id="f-goal" rows="2" placeholder="选好课型后会自动写一句；也可以自己改，或清空让系统按课型决定"></textarea>
        <div class="row" style="margin-top:6px">
          <button class="btn small" id="f-gen-goal">按课型生成</button>
          <button class="btn small" id="f-clear-goal">清空</button>
          <span class="hint" id="f-goal-hint"></span>
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>当前基础</label>
          <select id="f-level">${LEVELS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        </div>
        <div class="field">
          <label>内容深度</label>
          <select id="f-depth">${DEPTHS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        </div>
        <div class="field">
          <label>单元数量</label>
          <select id="f-units">
            <option value="" selected>自动（按材料定）</option>
            ${[2, 3, 4, 5, 6].map((n) => `<option value="${n}">${n} 个单元</option>`).join("")}
            <option value="custom">自定义…</option>
          </select>
          <input type="number" id="f-units-custom" min="1" max="12" placeholder="1 ~ 12"
                 style="display:none;margin-top:6px;max-width:110px">
        </div>
        <div class="field">
          <label>实践环节</label>
          <select id="f-hands">
            <option value="auto" selected>自动（按材料决定）</option>
            <option value="1">包含实操（可出真实操作类题目）</option>
            <option value="0">纯理论（不出实操题，讲稿也不布置操作任务）</option>
          </select>
        </div>
      </div>
      <details class="adv">
        <summary>高级选项（辅助课型 / 补充要求）</summary>
        <div class="field">
          <label>辅助课型（可选，最多一个）</label>
          <select id="f-assist"></select>
          <div class="hint" id="f-assist-hint">在满足主课型结构的前提下，额外加强某一块（例如主课型「精读」+ 辅助「考点」→ 逐句精讲并加强考点）。</div>
        </div>
        <div class="field">
          <label>补充要求（可选，一句话）</label>
          <input type="text" id="f-note" placeholder="例如：学生初三 / 2 课时 / 必须包含背诵默写">
        </div>
      </details>
      <div class="row" style="justify-content:flex-end">
        <button class="btn" id="f-cancel">取消</button>
        <button class="btn primary" id="f-go">生成大纲</button>
      </div>
      <div class="hint">生成过程会分阶段显示进度；生成完成后你可以先修改结构，再确认。</div>`;
    host.appendChild(card);

    const pick = document.getElementById("pick-docs");
    if (!S.documents.length) {
      pick.appendChild(el("div", "hint", "还没有已解析的材料，请先到「工作台」上传文件。"));
    }
    S.documents.forEach((d) => {
      // 标题包一层 span：多列网格下超长文件名用省略号，完整名放 title 里
      const lab = el("label", "switch",
        `<input type="checkbox" value="${d.id}">` +
        `<span class="t" title="${esc(d.title)}">${esc(d.title)}</span>`);
      pick.appendChild(lab);
    });
    // 已选计数：材料多时一眼知道选了几份（不选 = 用全部材料）
    const pickHint = el("div", "hint", "");
    pick.parentElement.appendChild(pickHint);
    const paintPicked = () => {
      const n = pick.querySelectorAll("input:checked").length;
      pickHint.textContent = n ? `已选 ${n} 份材料` : "";
    };
    pick.addEventListener("change", paintPicked);
    paintPicked();

    // ── 材料来源：用我的材料 / 没有材料（让 AI 先把材料写出来）──────
    // 两段式（用户拍板）：AI 写的材料**真的入库**，之后大纲/讲义/练习/引用走的还是老路，
    // 「只依据材料」与「引用防伪」两条红线因此都还在。
    const T = S.topicMode || (S.topicMode = {
      on: false, topic: "", depth: "standard", gid: "", outline: [],
      docId: "", title: "", stage: "",
    });
    const paneDocs = document.getElementById("pane-docs");
    const paneTopic = document.getElementById("pane-topic");
    const tHint = document.getElementById("t-hint");
    const tWrap = document.getElementById("t-outline-wrap");
    const tChapters = document.getElementById("t-chapters");

    const paintSrc = () => {
      paneDocs.style.display = T.on ? "none" : "";
      paneTopic.style.display = T.on ? "" : "none";
      document.getElementById("src-docs").classList.toggle("on", !T.on);
      document.getElementById("src-topic").classList.toggle("on", T.on);
      if (T.docId) {
        tHint.textContent = `✅ 材料已就绪：《${T.title}》，将作为本课的学习材料`;
      }
    };
    const paintChapters = () => {
      tWrap.style.display = T.outline.length ? "" : "none";
      tChapters.innerHTML = "";
      T.outline.forEach((c, i) => {
        const row = el("div", "row", "");
        row.style.marginBottom = "4px";
        const inp = el("input");
        inp.type = "text";
        inp.value = c.title || "";
        inp.style.flex = "1";
        inp.oninput = () => { T.outline[i].title = inp.value; };
        const del = el("button", "btn small", "删");
        del.type = "button";
        del.onclick = () => { T.outline.splice(i, 1); paintChapters(); scheduleDraftSave(); };
        row.append(inp, del);
        tChapters.appendChild(row);
      });
    };

    /** 轮询生成任务（材料目录 / 材料正文都用它）。 */
    const pollGen = async (gid, tick, done, fail) => {
      for (let i = 0; i < 900; i++) {
        // 用户切页后向导会被重建（S.topicMode 换成新对象）→ 旧轮询必须自己退出：
        // 否则它会一直打接口，还往已经不在页面上的节点写状态（幽灵轮询）。
        if (S.topicMode !== T) return;
        let d = null;
        try { d = await Api.get("/api/generations/" + gid); } catch (e) { /* 抖动就重试 */ }
        if (d) {
          if (d.status === "ready") return done(d);
          if (d.status === "failed") return fail(new Error(d.error || "生成失败"));
          tick(d);
        }
        await new Promise((r) => setTimeout(r, 700));
      }
      fail(new Error("生成超时，请稍后重试"));
    };

    document.getElementById("src-docs").onclick = () => { T.on = false; paintSrc(); scheduleDraftSave(); };
    document.getElementById("src-topic").onclick = () => { T.on = true; paintSrc(); scheduleDraftSave(); };

    const tOutlineBtn = document.getElementById("t-outline");
    tOutlineBtn.onclick = async () => {
      const topic = document.getElementById("f-topic").value.trim();
      if (!topic) return Toast("先写一句「想学什么」", true);
      T.topic = topic;
      T.depth = document.getElementById("f-topic-depth").value;
      T.outline = []; T.docId = ""; paintChapters();
      scheduleDraftSave();
      tOutlineBtn.disabled = true; tOutlineBtn.textContent = "生成目录中…";
      try {
        const r = await Api.post("/api/materials/outline", { topic, depth: T.depth });
        T.gid = r.generation_id;
        scheduleDraftSave();          // 记下 gid：切页后靠它接回这条任务
        await pollGen(T.gid,
          () => { tHint.textContent = "正在规划材料结构…"; },
          applyOutline,
          (e) => { tHint.textContent = "目录生成失败，可改主题后重试"; Toast("目录生成失败：" + e.message, true); });
      } catch (e) { Toast(e.message, true); }
      finally { tOutlineBtn.disabled = false; tOutlineBtn.textContent = "① 生成目录"; }
    };

    const tWriteBtn = document.getElementById("t-write");
    tWriteBtn.onclick = async () => {
      if (!T.gid || !T.outline.length) return Toast("请先生成目录", true);
      if (T.outline.some((c) => !String(c.title || "").trim())) return Toast("章节标题不能为空", true);
      tWriteBtn.disabled = true;
      try {
        await Api.post("/api/materials/chapters", { generation_id: T.gid, chapters: T.outline });
        scheduleDraftSave();          // 记下「正在写正文」，切页回来能接回这条轮询
        await pollGen(T.gid, paintWriting, finishWriting, failWriting);
      } catch (e) { Toast(e.message, true); }
      finally { tWriteBtn.disabled = false; tWriteBtn.textContent = "② 确认目录，开始写正文"; }
    };
    document.getElementById("t-add").onclick = () => {
      T.outline.push({ title: "", brief: "" });
      paintChapters();
      scheduleDraftSave();
    };

    /** 目录就绪：填进界面等用户审阅（①生成目录、以及接回「出目录中」的任务时共用）。 */
    const applyOutline = (d) => {
      const o = ((d.content_json || {}).outline) || {};
      T.title = o.title || T.topic || "";
      T.outline = (o.chapters || []).map((c) => ({ title: c.title, brief: c.brief }));
      tHint.textContent = `目录已生成（${T.outline.length} 章）。改好标题后点「② 确认目录，开始写正文」。`;
      paintChapters();
      scheduleDraftSave();
    };
    /** 写正文的进度：数 content_md 里的 `## `（每章一个，是服务端逐章追加的）。 */
    const paintWriting = (d) => {
      const total = T.outline.length || 1;
      const n = ((d.content_md || "").match(/^## /gm) || []).length;
      tHint.textContent = `正在写材料 ${Math.min(n + 1, total)}/${total} 章…（可切到别的页，回来自动继续）`;
    };
    /** 材料写完：记下 doc_id（它会自动成为本课的材料）。 */
    const finishWriting = (d) => {
      const cj = d.content_json || {};
      T.docId = cj.doc_id || "";
      T.title = cj.title || T.title;
      const dn = cj.chapters_done, tt = cj.chapters_total;
      const part = dn != null && tt != null && dn < tt;
      tHint.textContent = cj.skip_reason
        ? "ℹ️ " + cj.skip_reason
        : `✅ 材料已就绪：《${T.title}》（${dn || T.outline.length}/${tt || T.outline.length} 章）`
          + (part ? "，部分章节生成失败，可稍后重新生成材料" : "");
      if (T.docId) { loadDocuments(); loadMaterialJobs(); }
      scheduleDraftSave();     // 材料写完 → 把 docId 记进草稿（切页回来直接能建课）
    };
    const failWriting = (e) => Toast("材料生成失败：" + e.message, true);

    paintSrc(); paintChapters();

    // ── 接回上次的材料任务（从课程页「AI 材料」入口点进来的）──
    // 状态**全部来自后端任务行**：目录在 content_json.outline，进度在 content_md。
    // 所以切页、甚至关掉软件重开，都还能接上 —— 这才是「找不回来」的根治办法，
    // 而不是把希望寄托在前端内存里那份状态上（那份状态必然随着向导重建而消失）。
    const pend = S.pendingMaterial;
    S.pendingMaterial = null;
    if (pend) {
      const params = pend.params || {};
      const o = ((pend.content_json || {}).outline) || {};
      T.on = true;
      T.gid = pend.id || "";
      T.depth = params.depth || "standard";
      T.topic = params.topic || "";
      T.title = o.title || pend.title || T.topic;
      T.outline = (o.chapters || []).map((c) => ({ title: c.title, brief: c.brief }));
      T.docId = (pend.content_json || {}).doc_id || "";
      const topicEl = document.getElementById("f-topic");
      if (topicEl && T.topic) topicEl.value = T.topic;
      const depthEl = document.getElementById("f-topic-depth");
      if (depthEl) depthEl.value = T.depth;
      paintSrc(); paintChapters();
      if (pend.status === "running" && T.gid) {
        if (T.outline.length) {
          tHint.textContent = "上次的任务还在后台继续，正在接回…";
          pollGen(T.gid, paintWriting, finishWriting, failWriting);
        } else {
          tHint.textContent = "正在规划材料结构…（上次的任务还在后台跑）";
          pollGen(T.gid, () => { tHint.textContent = "正在规划材料结构…"; },
                  applyOutline, failWriting);
        }
      } else if (pend.status === "failed") {
        tHint.textContent = "上次的材料生成中断了：已写好的章节仍留在任务里，可改主题后重新开始。";
      } else if (T.docId) {
        tHint.textContent = `✅ 材料已就绪：《${T.title}》，将作为本课的学习材料`;
      } else if (T.outline.length) {
        tHint.textContent = "目录已就绪，点「② 确认目录，开始写正文」继续。";
      }
    }


    document.getElementById("f-cancel").onclick = closeCreate;

    // 「单元数量 → 自定义…」时展开数字输入（1~12；留空 = 自动）
    const unitsSel = document.getElementById("f-units");
    const unitsCustom = document.getElementById("f-units-custom");
    unitsSel.onchange = () => {
      const custom = unitsSel.value === "custom";
      unitsCustom.style.display = custom ? "" : "none";
      if (custom) unitsCustom.focus();
    };

    // ── 课型（学习意图）：主课型必选；辅助课型在「高级选项」里可选、最多一个 ──
    // 课型库只有后端一份（GET /api/courses/intents），这里只负责渲染与选择。
    const intentBox = document.getElementById("intent-cards");
    const assistSel = document.getElementById("f-assist");
    const goalEl = document.getElementById("f-goal");
    const hintEl = document.getElementById("f-goal-hint");
    let intents = [];
    const chosen = { primary: "", assist: "", note: "" };

    const paintIntents = () => {
      intentBox.innerHTML = "";
      if (!intents.length) {
        intentBox.textContent = "课型库加载失败，请刷新页面重试。";
        return;
      }
      intents.forEach((t) => {
        const c = el("button", "intent-card" + (chosen.primary === t.id ? " on" : ""));
        c.type = "button";
        c.innerHTML = `<b>${esc(t.name)}</b><div class="fit">${esc(t.fit)}</div>
          <div class="pace">${esc(t.pace || "")}</div>`;
        c.onclick = async () => {
          chosen.primary = t.id;
          if (chosen.assist === t.id) chosen.assist = "";   // 辅助不能与主相同
          paintIntents(); paintAssist();
          scheduleDraftSave();                              // 点卡片不触发 input/change → 显式存
          await autoGoal();                                 // 选课型即自动写目标
        };
        intentBox.appendChild(c);
      });
    };

    const paintAssist = () => {
      assistSel.innerHTML = '<option value="">不用辅助课型</option>' + intents
        .filter((t) => t.id !== chosen.primary)
        .map((t) => `<option value="${t.id}">${esc(t.name)} —— ${esc(t.fit)}</option>`)
        .join("");
      // 下拉会因「换主课型」把旧选项过滤掉：内部值必须同步清空，
      // 否则会出现「界面显示不用辅助、提交却带着旧辅助课型」的错位。
      if (chosen.assist && (chosen.assist === chosen.primary
        || !intents.some((t) => t.id === chosen.assist))) {
        chosen.assist = "";
      }
      assistSel.value = chosen.assist || "";
      assistSel.onchange = () => { chosen.assist = assistSel.value || ""; };
    };

    const setGoalHint = (s) => { hintEl.textContent = s || ""; };

    /** 按「材料 + 课型」写一句目的句（用户可改、可清空；清空后后端按课型兜底）。
     *
     * `goalSeq` 是**迟到响应防线**：连点不同课型卡时，先发出的请求可能后返回，
     * 不加判别就会把「上一个课型的目标」写进输入框，甚至覆盖用户刚手改的文本。
     * 规则：每次发起自增；用户手改 / 清空也自增（作废在途请求）；返回时序号不符即丢弃。
     */
    let goalSeq = 0;
    const autoGoal = async () => {
      if (!chosen.primary) return;
      const noteEl = document.getElementById("f-note");
      chosen.note = (noteEl && noteEl.value || "").trim();
      const seq = ++goalSeq;
      setGoalHint("正在按课型写目标…");
      try {
        const ids = [...pick.querySelectorAll("input:checked")].map((i) => i.value);
        const r = await Api.post("/api/courses/suggest-goal", {
          document_ids: ids, primary: chosen.primary,
          assist: chosen.assist || null, note: chosen.note || null,
        });
        if (seq !== goalSeq) return;                   // 已被更新的一次选择 / 手改取代
        goalEl.value = r.goal || "";
        setGoalHint(r.goal ? "已按课型生成，可自由修改或清空" : "");
        scheduleDraftSave();
      } catch (e) {
        if (seq !== goalSeq) return;
        setGoalHint("生成失败，可自己写一句，或留空让系统按课型决定");
      }
    };
    // 用户手改目标 → 作废在途的自动写目标（程序赋值不触发 input，不会误伤自己）
    goalEl.addEventListener("input", () => { goalSeq++; });

    document.getElementById("f-analyze").onclick = async () => {
      const btn = document.getElementById("f-analyze");
      const hint = document.getElementById("intent-hint");
      if (!S.documents.length) return Toast("还没有已解析的材料", true);
      btn.disabled = true; btn.textContent = "分析中…";
      hint.textContent = "正在看材料的体裁与内容，判断适合什么课…";
      try {
        const ids = [...pick.querySelectorAll("input:checked")].map((i) => i.value);
        const r = await Api.post("/api/courses/suggest-intents", { document_ids: ids });
        chosen.primary = r.primary || "";
        if (chosen.assist === chosen.primary) chosen.assist = "";  // 推荐结果可能撞上已选辅助
        paintIntents(); paintAssist();
        scheduleDraftSave();
        const name = (intents.find((t) => t.id === chosen.primary) || {}).name || "";
        const alts = (r.alternatives || [])
          .map((id) => (intents.find((t) => t.id === id) || {}).name).filter(Boolean);
        // 材料来源与理由**永远可见**：曾经「勾文言文却推荐出大模型目标」时界面上毫无线索。
        hint.textContent = `推荐「${name}」${r.reason ? "：" + r.reason : ""}`
          + (alts.length ? `（也可以试试：${alts.join("、")}）` : "")
          + ((r.sources || []).length ? ` ｜ 基于：${r.sources.map(esc).join("、")}` : "");
        await autoGoal();
      } catch (e) {
        hint.textContent = "";
        Toast("分析失败：" + e.message, true);
      } finally { btn.disabled = false; btn.textContent = "✨ 分析材料并预选"; }
    };

    document.getElementById("f-gen-goal").onclick = () => {
      if (!chosen.primary) return Toast("请先选一个课型", true);
      autoGoal();
    };
    document.getElementById("f-clear-goal").onclick = () => {
      goalSeq++;                       // 清空同样要作废在途请求，否则会被迟到的响应写回
      goalEl.value = "";
      setGoalHint("已清空 —— 留空时系统按课型决定这门课怎么讲");
      scheduleDraftSave();
    };

    (async () => {
      try {
        const r = await Api.get("/api/courses/intents");
        intents = r.items || [];
      } catch (e) { intents = []; }
      // 草稿里的课型要等课型库拉回来才能选中（否则 paintIntents 会把卡片画成「没选」）
      const d0 = loadDraft();
      if (d0 && d0.intentPrimary && intents.some((t) => t.id === d0.intentPrimary)) {
        chosen.primary = d0.intentPrimary;
        chosen.assist = (d0.intentAssist && d0.intentAssist !== d0.intentPrimary) ? d0.intentAssist : "";
      }
      paintIntents(); paintAssist();
      document.getElementById("intent-hint").textContent = chosen.primary
        ? "已恢复上次选的课型；要换就点别的卡。"
        : "点一张卡选课型；拿不准就点「分析材料并预选」。";
    })();

    document.getElementById("f-go").onclick = async () => {
      if (!chosen.primary) return Toast("请先选一个课型", true);
      // 目标允许留空（用户可一键清空）：后端会按所选课型兜底出一句「目的」。
      const goal = goalEl.value.trim();
      const badge = document.getElementById("model-badge");
      if (!badge.dataset.ok) return Toast("请先在「设置」里配置对话模型", true);
      // 材料来源：主题模式下用 AI 刚生成并入库的那份材料
      if (T.on && !T.docId) return Toast("请先点「① 生成目录」把材料写出来，或切回「用我上传的材料」", true);
      const ids = T.on
        ? [T.docId]
        : [...pick.querySelectorAll("input:checked")].map((i) => i.value);
      const btn = document.getElementById("f-go");
      btn.disabled = true; btn.textContent = "生成中…";
      try {
        const rawUnit = document.getElementById("f-units").value;
        const customUnit = parseInt((document.getElementById("f-units-custom") || {}).value || "", 10);
        const unitCount = rawUnit === "custom"
          ? (Number.isFinite(customUnit) ? customUnit : null)
          : (rawUnit ? parseInt(rawUnit, 10) : null);
        const rawHands = document.getElementById("f-hands").value;   // auto | 1 | 0
        const noteEl = document.getElementById("f-note");
        const r = await Api.post("/api/courses", {
          goal, document_ids: ids,
          level: document.getElementById("f-level").value,
          depth: document.getElementById("f-depth").value,
          unit_count: unitCount,
          // 「自动」传 null → 后端存 NULL，由模型按材料判断要不要出实操题
          hands_on: rawHands === "auto" ? null : rawHands === "1",
          intent: {
            primary: chosen.primary,
            assist: chosen.assist || null,
            note: ((noteEl && noteEl.value) || "").trim() || null,
          },
        });
        // 建课已发起 → 这份草稿的使命完成（后面的进度由大纲任务自己承载）
        clearDraft();
        await pollOutline(r.course_id, r.job_id, host);
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = "生成大纲";
      }
    };

    // ⚠️ 这段必须留在 renderCreate 的**末尾**：它读 goalEl / unitsSel / unitsCustom /
    // chosen / paintPicked —— 这些都是本函数里稍后才声明的 const，插在它们前面
    // 会直接踩 TDZ（ReferenceError: Cannot access 'goalEl' before initialization），
    // 整个视图会停在「加载中…」（2026-09-19 实测，就是被这条坑到的）。
    // 点了「AI 材料」里的任务 = 用户明确选定这条 → 立刻把它存成当前草稿，
    // 免得下次回来又把更早的旧记录当成「最新的」。
    if (pend) scheduleDraftSave();

    // ── 草稿：收集（任何变化都存一次）──────────────────────
    // 这一点是本次修复的核心：以前向导的进度只活在内存里，向导一重建就没了；
    // 而课程页唯一的「回来的路」只认后端材料任务，用上传材料建课的人根本没有它。
    const collectDraft = () => ({
      mode: T.on ? "topic" : "docs",
      docIds: [...pick.querySelectorAll("input:checked")].map((i) => i.value),
      intentPrimary: chosen.primary,
      intentAssist: chosen.assist,
      goal: goalEl.value,
      level: document.getElementById("f-level").value,
      depth: document.getElementById("f-depth").value,
      units: unitsSel.value,
      unitsCustom: unitsCustom.value,
      hands: document.getElementById("f-hands").value,
      note: document.getElementById("f-note").value,
      topic: {
        // ⚠️ 优先读**输入框的实时值**：T.topic / T.depth 只在点「① 生成目录」时才写入，
        // 若只读 T，用户手打了主题还没生成目录就切页 → 草稿里是空的（实测踩到）。
        topic: (document.getElementById("f-topic").value || T.topic || ""),
        depth: (document.getElementById("f-topic-depth").value || T.depth || "standard"),
        gid: T.gid || "", docId: T.docId || "", title: T.title || "",
        outline: (T.outline || []).map((c) => ({ title: c.title, brief: c.brief })),
      },
    });
    draftCollector = collectDraft;
    // 委托在卡片上：字段增删不用逐个绑定；程序改值的地方另外显式调一次
    card.addEventListener("input", scheduleDraftSave);
    card.addEventListener("change", scheduleDraftSave);

    /** 接回一条材料任务（草稿里存着 gid 时用；「AI 材料」入口那条走上面的 pend 分支）。 */
    const resumeMaterialJob = async (gid) => {
      let d = null;
      try { d = await Api.get("/api/generations/" + gid); } catch (e) { return; }
      if (S.topicMode !== T) return;            // 已经离开向导 → 不写幽灵状态
      const cj = d.content_json || {};
      const o = cj.outline || {};
      if (o.title) T.title = o.title;
      if ((o.chapters || []).length) {
        T.outline = o.chapters.map((c) => ({ title: c.title, brief: c.brief }));
      }
      if (cj.doc_id) T.docId = cj.doc_id;
      paintSrc(); paintChapters();
      if (d.status === "running") {
        if (T.outline.length) {
          tHint.textContent = "上次写正文的任务还在后台继续，正在接回…";
          pollGen(gid, paintWriting, finishWriting, failWriting);
        } else {
          tHint.textContent = "上次的任务还在规划目录，正在接回…";
          pollGen(gid, () => { tHint.textContent = "正在规划材料结构…"; }, applyOutline, failWriting);
        }
      } else if (d.status === "failed") {
        tHint.textContent = "上次的材料生成中断了：已写好的章节仍留在任务里，可改主题后重新开始。";
      } else if (T.docId) {
        tHint.textContent = `✅ 材料已就绪：《${T.title}》，将作为本课的学习材料`;
      } else if (T.outline.length) {
        tHint.textContent = "目录已就绪，点「② 确认目录，开始写正文」继续。";
      }
    };

    /** 把草稿填回界面。字段类立刻填；课型卡要等课型库拉回来（见下面的异步块）。 */
    const applyDraft = (d) => {
      if (!d) return;
      const t = d.topic || {};
      T.on = d.mode === "topic";
      T.topic = t.topic || "";
      T.depth = t.depth || "standard";
      T.gid = t.gid || "";
      T.docId = t.docId || "";
      T.title = t.title || "";
      T.outline = (t.outline || []).map((c) => ({ title: c.title, brief: c.brief }));
      document.getElementById("f-topic").value = T.topic;
      document.getElementById("f-topic-depth").value = T.depth;
      if ((d.docIds || []).length) {
        pick.querySelectorAll("input").forEach((i) => { i.checked = d.docIds.includes(i.value); });
      }
      paintPicked();
      goalEl.value = d.goal || "";
      document.getElementById("f-level").value = d.level || "beginner";
      document.getElementById("f-depth").value = d.depth || "standard";
      if (d.units === "custom") {
        unitsSel.value = "custom";
        unitsCustom.style.display = "";
        unitsCustom.value = d.unitsCustom || "";
      } else {
        unitsSel.value = d.units || "";
      }
      document.getElementById("f-hands").value = d.hands || "auto";
      document.getElementById("f-note").value = d.note || "";
      paintSrc(); paintChapters();
      if (T.gid) resumeMaterialJob(T.gid);      // 上次在写正文 → 自动接回
    };

    // ── 恢复：没有点「AI 材料」入口时，用本地草稿把进度填回去 ──
    if (!pend && draftMeaningful(draft)) {
      applyDraft(draft);
      const bar = el("div", "hint");
      bar.style.marginTop = "8px";
      bar.innerHTML = `↩ 已恢复上次未完成的创建（保存于 ${draftTimeText(draft.at)}）。`;
      const clr = el("button", "btn small", "清空草稿，重新开始");
      clr.type = "button";
      clr.style.marginLeft = "8px";
      clr.onclick = () => {
        clearDraft();
        S.topicMode = null;
        renderMain();
        Toast("已清空草稿，从头开始", false);
      };
      bar.appendChild(clr);
      card.insertBefore(bar, card.firstChild);
    }
  }

  /** 轮询大纲任务，完成后进入结构确认。 */
  /**
   * 轮询大纲任务直到完成。
   *
   * ``onReady`` / ``onFail`` 由调用方注入收尾动作：
   * - 建课向导 / 编辑结构：默认跳到结构确认页（confirmOutline）；
   * - 课程详情页的「重新生成大纲」：**不能**把整个详情换成结构编辑器，
   *   要原地重置按钮并刷新详情（否则按钮停在「重新生成中…」，状态与
   *   右侧 stage 文案「已完成」不同步 —— 用户实测问题 1）。
   * 轮询中对网络抖动容错（连续失败 >20 次才放弃），总时长上限提到 10 分钟
   * （新提示词 + 8k 输出下，多单元大纲可能超过旧的 210 秒）。
   */
  async function pollOutline(courseId, jobId, host, stageBox, onReady, onFail) {
    let misses = 0;
    for (let i = 0; i < 600; i++) {
      let job;
      try {
        job = await Api.get("/api/courses/jobs/" + jobId);
        misses = 0;
      } catch (e) {
        if (++misses > 20) {
          Toast("与服务的连接不稳定，请稍后在课程页查看结果", true);
          if (onFail) { onFail(e); return; }
          return;
        }
        await new Promise((r) => setTimeout(r, 1000));
        continue;
      }
      if (stageBox) stageBox.textContent = job.stage || "";
      else showStage(host, job.stage);
      if (job.status === "ready") {
        if (onReady) { await onReady(job); return; }
        await loadCourses();
        await loadMaterialJobs();   // 材料可能已被本课用掉 → 刷新「AI 材料」区块
        await loadCourse(courseId);
        renderList();
        S.creating = false;
        return confirmOutline(host, courseId);
      }
      if (job.status === "failed") {
        if (onFail) { onFail(new Error(job.error || "生成失败")); return; }
        host.innerHTML = `<div class="card"><b>大纲生成失败</b><div class="hint">${esc(job.error || "")}</div>
          <div class="row" style="margin-top:12px"><button class="btn primary" id="retry">重新生成</button></div></div>`;
        document.getElementById("retry").onclick = openCreate;
        return;
      }
      await new Promise((r) => setTimeout(r, 700));
    }
    Toast("生成超时，请稍后在课程页查看结果", true);
    if (onFail) onFail(new Error("生成超时"));
  }

  function showStage(host, stage) {
    host.innerHTML = `<div class="card"><b>正在生成课程大纲…</b>
      <div class="hint" style="margin-top:8px">${esc(stage || "")}</div>
      <div class="bar"><i style="width:100%"></i></div>
      <div class="hint">时间取决于模型速度，通常十几秒到一分钟。</div></div>`;
  }

  /** 讲次教学设计 desc → 折叠展示块（只读；无 desc 返回空串）。 */
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

  /** 结构确认：可改标题/目标、删除讲次，确认后进入课程。 */
  function confirmOutline(host, courseId) {
    const c = S.course;
    const draft = JSON.parse(JSON.stringify(c.units ? { title: c.title, units: c.units } : { units: [] }));
    draft.units = draft.units.map((u) => ({
      title: u.title, summary: u.summary,
      // desc / depth：大纲阶段生成的字段，**只读展示但必须原样带回**。
      // desc 跟着讲次对象走 → 删一讲、加两讲之后，各讲的教学设计仍挂在自己身上
      // （后端按序号位置复用行，若这里不带 desc 就会整体错位到别的讲次上）。
      lessons: u.lessons.map((l) => ({
        id: l.id || null,
        title: l.title, objective: l.objective, kind: l.kind,
        depth: l.depth, desc: l.desc || null,
      })),
    }));

    // 已有讲次 id 说明这门课已经落库过 → 本次是「编辑结构」而不是首次确认。
    const editing = (c.units || []).some((u) => (u.lessons || []).some((l) => l.id));
    const card = el("div", "card");
    card.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b>${editing ? "编辑课程结构" : "确认课程结构"}</b>
        <span class="pill">${editing ? "修改后保存即生效" : "第 2 步 / 共 2 步"}</span>
      </div>
      <div class="hint">左边改标题与学习目标、增删讲次；右边是同一份结构的思维导图，改完立即同步。</div>
      <div class="sep"></div>
      <div class="tree-layout">
        <div id="tree" class="tree-edit"></div>
        <div class="tree-map">
          <div class="row" style="justify-content:space-between">
            <span class="hint">结构预览（思维导图）</span>
            <button class="btn small" id="o-fit">适应窗口</button>
          </div>
          <div id="map"></div>
        </div>
      </div>
      <div class="field" style="margin-top:12px">
        <label>重新生成前，写下你的要求（可选）</label>
        <textarea id="o-regen-note" rows="2"
          placeholder="例如：单元再少一点，只保留 3 个；多放一些例题与易错点；先讲定义再讲计算"></textarea>
        <div class="hint">点「重新生成」时会把这段要求交给模型，新大纲会尽量按你的描述调整；留空则按当前目标重新生成。</div>
      </div>
      <div class="row" style="justify-content:flex-end">
        <button class="btn" id="o-regen">重新生成</button>
        <button class="btn primary" id="o-ok">${editing ? "保存结构" : "确认，开始学习"}</button>
      </div>`;
    host.appendChild(card);

    const tree = document.getElementById("tree");
    const paint = () => {
      tree.innerHTML = "";
      draft.units.forEach((u, ui) => {
        const box = el("div", "unit-box");
        box.appendChild(el("div", "row",
          `<input type="text" value="${esc(u.title)}" data-u="${ui}" style="flex:1">`));
        u.lessons.forEach((l, li) => {
          const row = el("div", "lesson-row");
          row.innerHTML = `<span class="pill">${l.kind === "practice" ? "练习" : "讲解"}</span>`;
          const t = el("input", null); t.type = "text"; t.value = l.title; t.style.flex = "1";
          t.oninput = () => { l.title = t.value; drawMap(); };
          const o = el("input", null); o.type = "text"; o.value = l.objective; o.style.flex = "1";
          o.placeholder = "学习目标";
          o.oninput = () => { l.objective = o.value; drawMap(); };
          const x = el("button", "btn small", "删除");
          x.onclick = () => { u.lessons.splice(li, 1); paint(); };
          row.appendChild(t); row.appendChild(o); row.appendChild(x);
          box.appendChild(row);
          const dd = descHtml(l.desc);
          if (dd) box.insertAdjacentHTML("beforeend", dd);
        });
        const add = el("button", "btn small", "＋ 讲次");
        add.onclick = () => {
          u.lessons.push({ title: "新的讲次", objective: "", kind: "lecture" });
          paint();
        };
        box.appendChild(add);
        box.querySelector(`[data-u="${ui}"]`).oninput = (e) => { u.title = e.target.value; drawMap(); };
        tree.appendChild(box);
      });
      drawMap();
    };

    /** 把当前草稿渲染为 markmap 思维导图（复用资料生成页的渲染方式）。 */
    function drawMap() {
      const box = document.getElementById("map");
      if (!box) return;
      const lines = [`# ${draft.title || "课程结构"}`, ""];
      draft.units.forEach((u) => {
        lines.push(`- ${u.title || "未命名单元"}`);
        u.lessons.forEach((l) => {
          const mark = l.kind === "practice" ? "（练习）" : "";
          lines.push(`  - ${l.title || "未命名讲次"}${mark}`);
        });
      });
      S.mindmap = MD.mindmap(box, lines.join("\n"));
    }
    paint();

    document.getElementById("o-fit").onclick = () => {
      try { S.mindmap && S.mindmap.fit(); } catch (e) { /* 忽略 */ }
    };

    document.getElementById("o-ok").onclick = async () => {
      const units = draft.units
        .filter((u) => u.lessons.length)
        .map((u) => ({
          title: u.title, summary: u.summary,
          lessons: u.lessons.map((l) => ({
            // 讲次 id：编辑已有课程时回带，后端按 id 复用行（内容跟着讲次走）。
            // 新建讲次没有 id → null → 后端按新增处理。
            id: l.id || null,
            title: l.title, objective: l.objective, kind: l.kind,
            // desc 必须带上：后端按讲次**对象**复用行，缺了它，被删讲次之后
            // 的每一讲都会继承前一讲的 desc（静默错位）。depth 只在新增讲次时被后端采用。
            depth: l.depth || "standard",
            desc: l.desc || null,
          })),
        }));
      if (!units.length) return Toast("至少保留一个讲次", true);
      const btn = document.getElementById("o-ok");
      btn.disabled = true; btn.textContent = "保存中…";
      try {
        await Api.post(`/api/courses/${courseId}/outline:confirm`, { title: c.title, units });
        S.creating = false;
        await loadCourses();
        await loadCourse(courseId);
        renderList();
        if (editing) {
          // 清掉 ?confirm=，避免刷新后又跳回编辑器
          location.hash = "#/courses";
          renderMain();
          Toast("结构已保存");
        } else {
          // 同样要清掉 ?new=1：否则刷新页面会又跳进新建向导（用户反馈过的「残留」）
          goHash("#/courses");
          renderMain();
          Toast("课程已就绪，开始学习吧");
        }
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = editing ? "保存结构" : "确认，开始学习";
      }
    };
    document.getElementById("o-regen").onclick = async () => {
      const btn = document.getElementById("o-regen");
      const noteEl = document.getElementById("o-regen-note");
      const note = (noteEl && noteEl.value || "").trim();
      btn.disabled = true; btn.textContent = "重新生成中…";
      // 立刻给出反馈，避免「点了没反应」的错觉。
      showStage(host, note ? "正在按你的要求重新组织大纲…" : "正在重新生成大纲…");
      try {
        const r = await Api.post(`/api/courses/${courseId}/outline:regenerate`, { note });
        await pollOutline(courseId, r.job_id, host);
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = "重新生成";
        renderMain();
      }
    };
  }

  /* ── 课程详情 ─────────────────────────── */
  function renderDetail(host) {
    // 详情页只承载一份内容：先清空再画。否则「重新生成大纲」完成后的
    // 原地刷新（onReady → renderDetail）会把新详情 append 在旧详情下面，
    // 出现新旧大纲上下并排（用户实测问题 1）。
    host.innerHTML = "";
    const c = S.course;
    const p = c.progress || {};
    // 讲次教学设计 desc：旧课程（功能上线前建的）为 null，详情页给一条「只补 desc」
    // 的入口 —— 用户若走「重新生成大纲」会重建讲次、把已生成好的讲义一起丢掉。
    const allLessons = [];
    (c.units || []).forEach((u) => (u.lessons || []).forEach((l) => allLessons.push(l)));
    const needDesc = allLessons.length > 0 && allLessons.some((l) => !l.desc);
    // 这门课备课用了哪些材料 —— 用户要求结构页能看出依据了哪些文件。
    // 标题从已加载的 S.documents 里查（课程详情只回 document_ids，不带标题）。
    const srcIds = c.document_ids || [];
    const srcNames = srcIds
      .map((id) => (S.documents || []).find((x) => x.id === id))
      .filter(Boolean)
      .map((d) => `《${esc(d.title || "材料")}》`);
    const srcHint = srcIds.length
      ? `<div class="hint" id="c-src">材料来源（${srcIds.length}）：${
          (srcNames.length > 4 ? srcNames.slice(0, 4).concat(`等 ${srcNames.length} 份`) : srcNames).join("、")
          || "（标题待加载）"}</div>`
      : "";
    const head = el("div", "card");
    head.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b style="font-size:16px">${esc(c.title)}</b>
        <span class="pill ${c.status === "ready" ? "ok" : ""}">${c.status === "ready" ? "已就绪" : c.status === "failed" ? "生成失败" : "生成中"}</span>
      </div>
      <div class="hint">目标：${esc(c.goal)}</div>
      <div class="hint">基础 ${esc(c.level_name)} · 深度 ${esc(c.depth_name)} · 共 ${c.units.length} 个单元</div>
      ${c.intent ? `<div class="hint">课型：${esc(c.intent.primary_name)}${c.intent.assist_name ? " ＋ " + esc(c.intent.assist_name) + "（辅助）" : ""}${c.intent.note ? " ｜ " + esc(c.intent.note) : ""}</div>` : ""}
      ${srcHint}
      ${c.summary ? `<div class="hint" style="margin-top:6px">${esc(c.summary)}</div>` : ""}
      <div class="bar" style="margin-top:10px"><i style="width:${p.percent || 0}%"></i></div>
      <div class="hint">进度：${p.done_lessons || 0}/${p.total_lessons || 0} 节（${p.percent || 0}%）</div>
      ${c.error ? `<div class="hint" style="color:var(--bad)">${esc(c.error)}</div>` : ""}
      <div class="row" style="margin-top:10px">
        <button class="btn small" id="c-edit">编辑结构</button>
        <button class="btn small" id="c-regen">重新生成大纲</button>
        ${needDesc ? '<button class="btn small" id="c-desc">补写教学设计</button>' : ""}
        <span class="hint" id="c-stage"></span>
      </div>
      ${needDesc ? `<div class="hint">本课建立时还没有「教学设计」功能，各讲课件只能看到标题。
        点「补写教学设计」会按现有结构为每讲补一份（不改结构、不影响已生成的讲义）。</div>` : ""}
      <div class="field" style="margin-top:10px">
        <label>重新生成前，写下你的要求（可选）</label>
        <textarea id="c-regen-note" rows="2"
          placeholder="例如：把单元拆得更细，每个单元只讲一个概念；多放一些典型例题"></textarea>
        <div class="hint">填写后点「重新生成大纲」，新大纲会按你的描述调整；留空则按当前目标重新生成。</div>
      </div>`;
    host.appendChild(head);
    document.getElementById("c-edit").onclick = () => {
      location.hash = "#/courses?confirm=" + c.id;
    };
    document.getElementById("c-regen").onclick = async () => {
      const stage = document.getElementById("c-stage");
      const btn = document.getElementById("c-regen");
      const noteEl = document.getElementById("c-regen-note");
      const note = (noteEl && noteEl.value || "").trim();
      btn.disabled = true; btn.textContent = "重新生成中…";
      if (stage) stage.textContent = note ? "正在按你的要求重新组织大纲…" : "正在重新生成大纲…";
      const resetBtn = () => {
        const b = document.getElementById("c-regen");
        if (b) { b.disabled = false; b.textContent = "重新生成大纲"; }
        const st = document.getElementById("c-stage");
        if (st) st.textContent = "";
      };
      try {
        const r = await Api.post(`/api/courses/${c.id}/outline:regenerate`, { note });
        // 详情页的重新生成：完成后**原地刷新详情**（新大纲/进度直接可见），
        // 按钮复位 —— 不跳结构编辑器，避免「按钮停在生成中、状态却已完成」的不同步。
        await pollOutline(c.id, r.job_id, host, stage,
          async () => {
            resetBtn();
            await loadCourses();
            await loadCourse(c.id);
            renderDetail(host);
            Toast("大纲已重新生成", false);
          },
          () => resetBtn());
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = "重新生成大纲";
        if (stage) stage.textContent = "";
      }
    };

    // 「补写教学设计」：只为旧课程补 desc，不重建讲次、不动已生成的讲义。
    const descBtn = document.getElementById("c-desc");
    if (descBtn) {
      descBtn.onclick = async () => {
        const stage = document.getElementById("c-stage");
        descBtn.disabled = true; descBtn.textContent = "补写中…";
        if (stage) stage.textContent = "正在为每讲补写教学设计…";
        const reset = () => {
          const b = document.getElementById("c-desc");
          if (b) { b.disabled = false; b.textContent = "补写教学设计"; }
          const st = document.getElementById("c-stage");
          if (st) st.textContent = "";
        };
        try {
          const r = await Api.post(`/api/courses/${c.id}/desc:rebuild`);
          await pollOutline(c.id, r.job_id, host, stage,
            async () => {
              await loadCourse(c.id);
              renderDetail(host);
              Toast("教学设计已补写完成，展开各讲即可查看", false);
            },
            (err) => { Toast((err && err.message) || "补写失败，请稍后重试", true); reset(); });
        } catch (e) {
          Toast(e.message, true);
          reset();
        }
      };
    }

    // 按单元顺序 × 单元内讲次顺序，找出第一个 status !== "done" 的讲次（接下来要上的那一讲）
    let nextId = null;
    for (const u0 of (c.units || [])) {
      for (const l0 of (u0.lessons || [])) {
        if (l0.status !== "done") { nextId = l0.id; break; }
      }
      if (nextId) break;
    }

    (c.units || []).forEach((u) => {
      const box = el("div", "card");
      const tag = u.status === "done" ? '<span class="pill ok">已完成</span>'
        : u.status === "active" ? '<span class="pill">进行中</span>' : '<span class="pill">未开始</span>';
      const st = u.stats || {};
      box.innerHTML = `<div class="row" style="justify-content:space-between">
          <b>第 ${u.ordinal} 单元 · ${esc(u.title)}</b>${tag}</div>
        ${u.summary ? `<div class="hint">${esc(u.summary)}</div>` : ""}
        ${st.questions
          ? `<div class="hint">练习：答对 ${st.correct}/${st.questions} · 得分 ${st.score}${
              st.errors ? ` · 错题 ${st.errors}` : ""}</div>`
          : ""}`;
      const list = el("div");
      (u.lessons || []).forEach((l) => {
        const isNext = (l.id === nextId);
        const row = el("div", "item lesson-row" + (isNext ? " next-lesson" : ""));
        const done = l.status === "done";
        row.innerHTML = `<span class="pill">${esc(l.kind_name)}</span>
          ${isNext ? '<span class="pill next-pill">接下来</span>' : ""}
          <span class="t">${l.ordinal}. ${esc(l.title)}</span>
          ${done ? '<span class="pill ok">已完成</span>' : ""}`;
        const go = el("button", "btn small" + (isNext ? " primary" : ""), done ? "再看一遍" : "开始");
        go.onclick = () => { location.hash = "#/lessons/" + l.id; };
        row.appendChild(go);
        list.appendChild(row);
        // 本讲教学设计（大纲阶段生成，只读）：与结构确认页/讲义页同格式的折叠块。
        // 放在讲次行**之外**——.item 是 flex 行，塞进去会打乱 pill/标题/按钮的排布。
        const d = descHtml(l.desc);
        if (d) {
          const dw = el("div", "lesson-desc-row");
          dw.innerHTML = d;
          list.appendChild(dw);
        }
      });
      box.appendChild(list);

      // 单元总结入口（P1：学完一个单元后的回顾与薄弱点）
      const lastLesson = (u.lessons || [])[(u.lessons || []).length - 1];
      const foot = el("div", "row");
      foot.style.marginTop = "8px";
      const sum = el("button", "btn small",
        u.summary_status === "ready" ? "查看单元总结" : "生成单元总结");
      sum.onclick = () => {
        if (!lastLesson) return Toast("这个单元还没有讲次", true);
        location.hash = "#/lessons/" + lastLesson.id + "?tab=summary";
      };
      foot.appendChild(sum);
      if (u.summary_status === "ready") {
        foot.appendChild(el("span", "pill ok", "总结已生成"));
      }
      box.appendChild(foot);
      host.appendChild(box);
    });
  }

  /* ── 装配 ─────────────────────────────── */
  /** 从 hash 查询串读取要编辑结构的课程 id（#/courses?confirm=xxx）。 */
  function confirmTarget() {
    const q = location.hash.split("?")[1] || "";
    const m = /(?:^|&)confirm=([0-9a-fA-F]+)/.exec(q);
    return m ? m[1] : "";
  }

  async function render(host) {
    await Promise.all([loadCourses(), loadDocuments(), loadMaterialJobs()]);
    host.innerHTML = `
      <div class="cols">
        <div class="col col-side" id="course-list" style="display:flex;flex-direction:column"></div>
        <div class="col col-main page" id="course-main"></div>
      </div>`;
    renderList();
    if (S.courseId) { try { await loadCourse(S.courseId); } catch (e) { S.courseId = null; S.course = null; } }
    // 支持 #/courses?new=1 直接进入新建向导（便于 UI 冒烟测试）。
    // 反向也要管住：向导状态只由「本次进入是否带 new=1」决定 —— 否则用户不走「取消」
    // 而是切到别的页再回到课程页时，残留的 S.creating=true 会把他**又**丢进新建向导。
    if (/\bnew=1\b/.test(location.hash.split("?")[1] || "")) { S.creating = true; }
    else { S.creating = false; }

    // 直接进入结构编辑（课程详情里的「编辑结构」入口，也可用链接直达）
    const target = confirmTarget();
    if (target) {
      try {
        await loadCourse(target);
        S.creating = false;
        renderList();
        return confirmOutline(document.getElementById("course-main"), target);
      } catch (e) {
        Toast("打开结构编辑失败：" + e.message, true);
      }
    }
    renderMain();
  }

  window.Views = window.Views || {};
  window.Views.courses = { render };
})();
