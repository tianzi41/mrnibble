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
  // 学习目标框占位文案（单一来源：HTML 模板与「正在生成」临时占位共用，避免多处字面量）
  const GOAL_PLACEHOLDER = "选好课型后会自动写一句；也可以自己改，或清空让系统按课型决定";
  const GOAL_WRITING = "正在按课型写目标…";

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
  const DRAFT_KEY = "mrnibble-create-draft";
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
    return !!(d.job              // 大纲正在生成中（课程已建、只在等模型）
      || d.intentPrimary || String(d.goal || "").trim()
      || (d.docIds || []).length || t.topic || t.gid || t.docId
      || String(d.note || "").trim());
  }
  /** 草稿条目的标题：正在生成大纲时显示课程，否则显示主题/目标首句。 */
  function draftEntryTitle(d) {
    if (d && d.job) return `正在生成大纲：《${(d.job.title || "新课程").slice(0, 18)}》`;
    return draftTitle(d);
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
  /** 大纲生成失败时，把「正在生成」标记从草稿里摘掉 —— 表单内容原样留着，
   *  用户重进向导就能直接改改再点「生成大纲」，而不会被那条死掉的 job 反复弹回进度页。 */
  function dropDraftJob() {
    const d = loadDraft();
    if (!d || !d.job) return;
    delete d.job; delete d.at; delete d.v;
    saveDraft(d);
  }

  /* ── 「编辑课程结构」的未保存改动（本地保存）──────────────────
   *
   * 用户实测问题（2026-09-19）：在结构编辑页改到一半切到别的页，再从左栏点这门课
   * → 进的是课程详情，那个结构页面不见了，**改过的内容也全丢了**。
   * 原因：结构编辑页只挂在 `#/courses?confirm=<id>` 这条路由上（设计如此），
   * 而改动只活在内存里（真缺陷）。
   * 现在把改动按课程存成一份本地草稿：① 左栏点这门课直接回到结构编辑页；
   * ② 课程页给一条「正在编辑结构：《…》」入口（带 ✕ 可丢弃）；③ 保存成功后清掉。
   */
  const CONFIRM_KEY = "mrnibble-confirm-draft";
  let confirmSaver = null;      // confirmOutline 渲染时把自己的「保存函数」挂上来
  let confirmTimer = null;

  function saveConfirmDraft(o) {
    try {
      localStorage.setItem(CONFIRM_KEY, JSON.stringify({ v: 1, at: Date.now(), ...o }));
    } catch (e) { /* 隐私模式等，忽略 */ }
  }
  function loadConfirmDraft() {
    try {
      const raw = localStorage.getItem(CONFIRM_KEY);
      if (!raw) return null;
      const d = JSON.parse(raw);
      return (d && d.v === 1 && d.courseId) ? d : null;
    } catch (e) { return null; }
  }
  /** 清掉这份改动记录，**并取消在途的延时保存** —— 否则用户点 ✕ / 保存成功后，
   *  400ms 前排队的那次保存会把刚清掉的草稿又写回来（表现为「删了又出现」）。 */
  function clearConfirmDraft() {
    if (confirmTimer) { clearTimeout(confirmTimer); confirmTimer = null; }
    confirmSaver = null;
    try { localStorage.removeItem(CONFIRM_KEY); } catch (e) { /* 忽略 */ }
  }
  /** 结构编辑里的改动很碎（每个输入框 oninput 都触发）→ 400ms 合并一次。 */
  function scheduleConfirmSave() {
    if (!confirmSaver) return;
    if (confirmTimer) clearTimeout(confirmTimer);
    confirmTimer = setTimeout(() => {
      try { confirmSaver && confirmSaver(); } catch (e) { confirmSaver = null; }
    }, 400);
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
        if ((p.total_lessons || 0) > 0 && (p.done_lessons || 0) >= p.total_lessons) {
          item.appendChild(el("span", "pill ok", "已学完"));
        }
      const del = el("button", "x", "✕");
      del.title = "删除课程";
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除课程《${c.title}》？单元、讲次与练习记录会一并删除。`)) return;
        await Api.del("/api/courses/" + c.id);
        // 课程没了，别再留着指向它的「未保存改动 / 未完成创建」
        const cd = loadConfirmDraft();
        if (cd && cd.courseId === c.id) clearConfirmDraft();
        const dr = loadDraft();
        if (dr && dr.job && dr.job.courseId === c.id) dropDraftJob();
        if (S.courseId === c.id) { S.courseId = null; S.course = null; }
        await loadCourses(); renderList(); renderMain();
      };
      item.appendChild(del);
      // ⚠️ 必须走 openCourse：它会清掉 S.creating。
      // 只调 renderMain() 的话，向导状态还在 → renderMain 又把向导顶出来 →
      // 用户点左侧任何课程都进不去，看起来就是「锁死在新建课程界面」（2026-09-17 实测）。
      // ⚠️ 另外：这门课若有**未保存的结构改动**，点它要直接回结构编辑页 ——
      // 用户实测「改到一半切页，再点课程进来是详情页，结构页不见了、改动也没了」。
      item.onclick = () => {
        const cd = loadConfirmDraft();
        if (cd && cd.courseId === c.id) {
          S.creating = false; S.pendingMaterial = null;
          const h = "#/courses?confirm=" + c.id;
          if (location.hash === h) renderMain();     // 同 hash 不会派发 hashchange
          else location.hash = h;
          return;
        }
        openCourse(c.id);
      };
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
      it.title = d.job ? "点击回到生成进度界面" : "点击回到新建向导，继续上次未完成的创建";
      it.appendChild(el("span", "t", esc(draftEntryTitle(d))));
      // 「生成中」必须跟随**真实状态**：草稿里的 job 标记在任务失败后不会自己消失，
      // 只认它会让列表永远写着「生成中」（用户实测「一直显示创建中、也不说原因」）。
      const jc = d.job ? (S.courses || []).find((c) => c.id === d.job.courseId) : null;
      const state = !d.job ? draftTimeText(d.at)
        : (jc && jc.status === "ready") ? "已生成"
          : (jc && jc.status === "failed") ? "生成失败"
            : "生成中";
      it.appendChild(el("small", null, state));
      // ✕：用户要求这条也能自己删掉（不自动删——删什么由用户决定）
      const dx = el("button", "x", "✕");
      dx.title = d.job
        ? "删掉这条记录（课程已建好，左侧课程列表里还能找到它）"
        : "删掉这条未完成的创建（已填的表单内容会一起清掉）";
      dx.onclick = (ev) => {
        ev.stopPropagation();
        const msg = d.job
          ? "删掉这条「正在生成大纲」记录？\n\n课程本身已经建好了，左侧课程列表里还能找到它；"
            + "只是不再显示这条提示。"
          : "删掉这条未完成的创建？\n\n你已经填的内容会一起清掉，无法恢复。";
        if (!confirm(msg)) return;
        clearDraft();
        renderList();
        Toast("已删掉这条未完成的创建", false);
      };
      it.appendChild(dx);
      it.onclick = () => openCreate();
      pbody.appendChild(it);
      panel.appendChild(pbody);
      box.appendChild(panel);
    }
    // 「正在编辑结构」入口：结构页只挂在 ?confirm= 路由上，改动也只活在内存里 ——
    // 不给入口 + 不存改动，用户切页后既回不去、改动也没了（用户实测）。
    const cd = loadConfirmDraft();
    if (cd && cd.courseId) {
      const panel = el("div");
      panel.style.marginTop = "10px";
      panel.appendChild(el("div", "panel-head", "<span>正在编辑结构</span>"));
      const pbody = el("div", "panel-body");
      const it = el("div", "item");
      it.title = "点击回到结构编辑页，继续改（改动已本地保留）";
      it.appendChild(el("span", "t", esc(`《${cd.title || "课程"}》`)));
      it.appendChild(el("small", null, draftTimeText(cd.at)));
      const x = el("button", "x", "✕");
      x.title = "丢弃这份未保存的结构改动";
      x.onclick = (ev) => {
        ev.stopPropagation();
        if (!confirm("丢弃这份未保存的结构改动？\\n\\n课程本身不受影响，"
          + "下次进结构编辑页会显示服务端已保存的版本。")) return;
        clearConfirmDraft();
        renderList();
        Toast("已丢弃未保存的结构改动", false);
      };
      it.appendChild(x);
      it.onclick = () => {
        const h = "#/courses?confirm=" + cd.courseId;
        if (location.hash === h) renderMain();
        else location.hash = h;
      };
      pbody.appendChild(it);
      panel.appendChild(pbody);
      box.appendChild(panel);
    }
    // 有未完成的草稿时，「AI 材料」默认折叠：……
    renderMaterialJobs(box, { collapsed: draftMeaningful(d) });
  }

  /** 课程列表下方的「AI 材料」区块：只显示**还没收尾**的材料生成任务。
   *
   *  为什么需要它：材料是逐章写的、要跑几十秒，用户中途切到别的页是常态。
   *  此前进度只活在新建向导的内存里，向导一重建就再也找不回来
   *  （用户实测：「写正文到一半切到别的页再回来，我找不回来了」）。
   *  这里改成**从后端任务行恢复**：`GET /api/generations?type=material` 的任务里有
   *  目录 JSON 和「已写了几章」，所以切页、甚至关掉软件重开都能接上。
   */
  function renderMaterialJobs(box, opts) {
    opts = opts || {};
    // 最新在前（后端已 ORDER BY created_at DESC，这里再兜一次底，防以后改排序）
    const jobs = (S.materialJobs || []).filter(materialJobVisible)
      .slice()
      .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    if (!jobs.length) return;

    const panel = el("div");
    panel.style.marginTop = "10px";
    const head = el("div", "panel-head");
    head.appendChild(el("span", null, `AI 材料（${jobs.length}）`));
    const foldBtn = el("button", "btn small", "");
    foldBtn.type = "button";
    head.appendChild(foldBtn);
    panel.appendChild(head);

    const body = el("div", "panel-body");
    panel.appendChild(body);

    /** 一行材料任务：点它=继续/用它建课；右侧 ✕=删掉这条记录（用户自己点，不自动删）。 */
    const row = (j) => {
      const it = el("div", "item");
      it.title = "点击继续 / 用它建课";
      it.appendChild(el("span", "t", esc(materialJobTitle(j))));
      it.appendChild(el("small", null, materialJobBadge(j)));
      const del = el("button", "x", "✕");
      del.title = "删掉这条材料任务记录（已建课程引用的材料不受影响）";
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删掉「${materialJobTitle(j)}」这条材料任务记录？\\n\\n`
          + "只是去掉这条未收尾的记录；已经建好的课程与它用的材料不受影响。")) return;
        try {
          await Api.del("/api/generations/" + j.id);
        } catch (e) { Toast("删除失败：" + e.message, true); return; }
        await loadMaterialJobs();
        renderList();
        Toast("已删掉这条材料任务", false);
      };
      it.appendChild(del);
      it.onclick = () => { S.pendingMaterial = j; openCreate(); };
      return it;
    };

    let collapsed = !!opts.collapsed;      // 有未完成草稿时默认折叠（避免误点在旧记录上）
    let showOlder = false;
    const paint = () => {
      body.innerHTML = "";
      foldBtn.textContent = collapsed ? "展开 ▾" : "收起 ▴";
      if (collapsed) return;
      // 只默认展开**最新一条**：更早的多半是早已不再收尾的旧任务，全摊开会把最新的埋掉
      body.appendChild(row(jobs[0]));
      if (jobs.length > 1) {
        const n = jobs.length - 1;
        if (showOlder) jobs.slice(1).forEach((j) => body.appendChild(row(j)));
        const more = el("button", "btn small",
          showOlder ? `收起更早的 ${n} 条 ▴` : `更早的 ${n} 条 ▾`);
        more.type = "button";
        more.style.marginTop = "6px";
        more.onclick = () => { showOlder = !showOlder; paint(); };
        body.appendChild(more);
      }
    };
    foldBtn.onclick = () => { collapsed = !collapsed; paint(); };
    paint();
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
    // 上次点了「生成大纲」还没出结果 → 直接回到那个进度界面并续上轮询
    // （用户实测：点了生成大纲后切页，就再也回不到这个界面了）。
    if (draft && draft.job && draft.job.courseId && draft.job.jobId) {
      resumeOutlineJob(host, draft.job);
      return;
    }
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
            <div class="row" style="margin-top:6px;align-items:center">
              <button class="btn small" id="t-add" type="button">＋ 加一章</button>
              <button class="btn small primary" id="t-write" type="button">② 确认目录，开始写正文</button>
              <span id="t-status" class="run-status"></span>
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
        <textarea id="f-goal" rows="2" placeholder="${GOAL_PLACEHOLDER}"></textarea>
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
    const tStatus = document.getElementById("t-status");
    /** 材料生成（写正文）的进度/结果反馈，统一收敛到 #t-status（按钮右侧）。
     *  kind ∈ "writing" | "done" | "warn" | ""（空 = 不显示）。 */
    const setStatus = (text, kind) => {
      if (!tStatus) return;
      tStatus.textContent = text || "";
      tStatus.className = "run-status" + (kind ? " " + kind : "");
    };

    /** 本课材料 id 的**单一来源**：课型分析、目标自动填写、草稿都从这里取。
     *
     * 主题模式下唯一正确的材料就是 AI 刚写入库的那份（T.docId）——
     * #pick-docs 在主题模式下不仅整块隐藏，而且 AI 材料写完后 loadDocuments()
     * 并不会给它补复选框（pick 是建向导时按当时的 S.documents 一次性画好的），
     * 从那里取只会得到空数组或切页前残留的旧勾选。后端 _material_context 又把
     * 空数组兜底成「全部已解析材料」→ 拿用户库里的旧材料写出与本题材无关的目标
     * （实测：题材是 cmd/PowerShell，目标却填成「读懂《出师表》全文……」）。
     */
    const materialIds = () => {
      if (T.on) return T.docId ? [T.docId] : [];
      return [...pick.querySelectorAll("input:checked")].map((i) => i.value);
    };

    const paintSrc = () => {
      paneDocs.style.display = T.on ? "none" : "";
      paneTopic.style.display = T.on ? "" : "none";
      document.getElementById("src-docs").classList.toggle("on", !T.on);
      document.getElementById("src-topic").classList.toggle("on", T.on);
      if (T.docId) {
        setStatus(`✅ 材料已就绪：《${T.title}》，将作为本课的学习材料`, "done");
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
      setStatus("");   // 重开一轮：清掉上一轮残留的「材料已就绪」，免得新目录出来时顶着旧状态
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
      setStatus(`正在写材料 ${Math.min(n + 1, total)}/${total} 章…（可切到别的页，回来自动继续）`, "writing");
    };
    /** 材料写完：记下 doc_id（它会自动成为本课的材料）。 */
    const finishWriting = (d) => {
      const cj = d.content_json || {};
      T.docId = cj.doc_id || "";
      T.title = cj.title || T.title;
      const dn = cj.chapters_done, tt = cj.chapters_total;
      const part = dn != null && tt != null && dn < tt;
      setStatus(cj.skip_reason
        ? "ℹ️ " + cj.skip_reason
        : `✅ 材料已就绪：《${T.title}》（${dn || T.outline.length}/${tt || T.outline.length} 章）`
          + (part ? "，部分章节生成失败，可稍后重新生成材料" : ""), "done");
      if (T.docId) { loadDocuments(); loadMaterialJobs(); }
      scheduleDraftSave();     // 材料写完 → 把 docId 记进草稿（切页回来直接能建课）
      // 材料就绪后补一次目标：用户可能先选了课型、那时材料还没写好
      // （autoGoal 会提示「材料写好后…」并跳过）—— 现在材料有了，按课型补上。
      // chosen / goalEl 在下方定义，但这里只在轮询回调里运行（IIFE 早已求值完），无 TDZ 问题。
      if (chosen.primary && !goalEl.value.trim()) autoGoal();
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
          setStatus("上次的任务还在后台继续，正在接回…", "writing");
          pollGen(T.gid, paintWriting, finishWriting, failWriting);
        } else {
          // 目录还没出来时 #t-outline-wrap 是隐藏的（状态节点在它里面）→ 这条退回 tHint 才看得见
          tHint.textContent = "正在规划材料结构…（上次的任务还在后台跑）";
          pollGen(T.gid, () => { tHint.textContent = "正在规划材料结构…"; },
                  applyOutline, failWriting);
        }
      } else if (pend.status === "failed") {
        setStatus("上次的材料生成中断了：已写好的章节仍留在任务里，可改主题后重新开始。", "warn");
      } else if (T.docId) {
        setStatus(`✅ 材料已就绪：《${T.title}》，将作为本课的学习材料`, "done");
      } else if (T.outline.length) {
        tHint.textContent = "目录已就绪，点「② 确认目录，开始写正文」继续。";
        setStatus("");
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
      // 进入「正在生成」可视态：输入框主色描边 + 呼吸；空框时占位也提示「正在写」
      goalEl.classList.add("loading");
      if (!goalEl.value.trim()) goalEl.placeholder = GOAL_WRITING;
      try {
        // 主题模式下材料没写好就没有正确上下文可发：不发请求（空数组会被后端
        // 兜底成全部已解析材料 → 按库里旧材料写出无关目标），只提示；
        // 材料写好后由 finishWriting 补触发一次。return 放在 try 里，finally
        // 照常负责退 loading 态。
        if (T.on && !T.docId) {
          setGoalHint("材料写好后，会按所选课型自动写目标");
          return;
        }
        const ids = materialIds();
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
      } finally {
        // 本次请求仍是「当前有效」的那一条才退 loading；被作废的由胜出方负责退
        if (seq === goalSeq) {
          goalEl.classList.remove("loading");
          goalEl.placeholder = GOAL_PLACEHOLDER;
        }
      }
    };
    // 用户手改目标 → 作废在途的自动写目标（程序赋值不触发 input，不会误伤自己）
    goalEl.addEventListener("input", () => {
      goalSeq++;                                   // 手改作废在途自动写目标
      goalEl.classList.remove("loading");          // 用户已亲自接手 → 退出「正在生成」态
      goalEl.placeholder = GOAL_PLACEHOLDER;
    });

    document.getElementById("f-analyze").onclick = async () => {
      const btn = document.getElementById("f-analyze");
      const hint = document.getElementById("intent-hint");
      if (!S.documents.length) return Toast("还没有已解析的材料", true);
      // 主题模式下同样必须基于 AI 刚生成的那份材料：没写好就分析，只会拿到
      // 库里的旧材料（与 autoGoal 同一个坑）。
      if (T.on && !T.docId) return Toast("请先点「① 生成目录」把材料写出来，再分析推荐课型", true);
      btn.disabled = true; btn.textContent = "分析中…";
      hint.textContent = "正在看材料的体裁与内容，判断适合什么课…";
      try {
        const ids = materialIds();
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
      goalEl.classList.remove("loading");
      goalEl.placeholder = GOAL_PLACEHOLDER;
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
        // 课程已建、大纲正在生成 → 把「这次创建」记进草稿（含 course_id / job_id）：
        // 切到别的页、甚至关掉软件重开，都能回到进度界面继续等（用户实测：以前一
        // 切走就再也找不回来，只能在课程列表里看到一条「生成中」）。
        const d = collectDraft();
        d.job = { courseId: r.course_id, jobId: r.job_id, at: Date.now(), title: goal || "新课程" };
        saveDraft(d);
        await pollOutline(r.course_id, r.job_id, host, null,
          () => afterOutlineReady(host, r.course_id),
          () => dropDraftJob());
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = "生成大纲";
      }
    };

    // ⚠️ 这段必须留在 renderCreate 的**末尾**：它读 goalEl / unitsSel / unitsCustom /
    // chosen / paintPicked —— 这些都是本函数里稍后才声明的 const，插在它们前面
    // 会直接踩 TDZ（ReferenceError: Cannot access 'goalEl' before initialization），
    // 整个视图会停在「加载中…」（2026-09-19 实测，就是被这条坑到的）。
    // 从「AI 材料」接回一条旧任务时**不写草稿**：用户实测「点了那条旧记录，
    // 结果它把课程内容复制到我未完成的创建中去了」—— 那是把别人的进度顶掉了。
    // 改成只提示，并留一个「回到我未完成的创建」的出口（草稿一直原样留着）。
    if (pend) {
      const bar = el("div", "hint");
      bar.style.marginTop = "8px";
      bar.innerHTML = `↩ 正在继续 AI 材料《${esc(T.title || "材料")}》——它会作为这门课的学习材料。`;
      if (draftMeaningful(draft)) {
        const back = el("button", "btn small", "回到我未完成的创建");
        back.type = "button";
        back.style.marginLeft = "8px";
        back.onclick = () => { S.topicMode = null; renderMain(); };
        bar.appendChild(back);
      }
      card.insertBefore(bar, card.firstChild);
    }

    // ── 草稿：收集（任何变化都存一次）──────────────────────
    // 这一点是本次修复的核心：以前向导的进度只活在内存里，向导一重建就没了；
    // 而课程页唯一的「回来的路」只认后端材料任务，用上传材料建课的人根本没有它。
    const collectDraft = () => ({
      mode: T.on ? "topic" : "docs",
      docIds: materialIds(),
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
          setStatus("上次写正文的任务还在后台继续，正在接回…", "writing");
          pollGen(gid, paintWriting, finishWriting, failWriting);
        } else {
          // 同上：目录未出时 wrap 隐藏，状态写 #t-status 看不见
          tHint.textContent = "上次的任务还在规划目录，正在接回…";
          pollGen(gid, () => { tHint.textContent = "正在规划材料结构…"; }, applyOutline, failWriting);
        }
      } else if (d.status === "failed") {
        setStatus("上次的材料生成中断了：已写好的章节仍留在任务里，可改主题后重新开始。", "warn");
      } else if (T.docId) {
        setStatus(`✅ 材料已就绪：《${T.title}》，将作为本课的学习材料`, "done");
      } else if (T.outline.length) {
        tHint.textContent = "目录已就绪，点「② 确认目录，开始写正文」继续。";
        setStatus("");
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
      // 用户切页后向导会被重建（host 被替换、脱离文档）→ 旧轮询必须自己退出：
      // ① 否则它会一直打接口、还会往已经不在页面上的节点写 DOM；
      // ② 更糟的是「完成」时它会把草稿里的 job 标记清掉 —— 用户切页回来就
      //    再也找不到这个进度界面了（2026-09-19 用户实测「找不回来」的真凶）。
      // 停掉后草稿里的 job 标记会保留，回来点一下就能续上。
      if (host && !host.isConnected) return;
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
        if (!host || !host.isConnected) return;   // 已切走：只刷数据，别碰 DOM
        await loadCourse(courseId);
        renderList();
        S.creating = false;
        return confirmOutline(host, courseId);
      }
      if (job.status === "failed") {
        if (onFail) { onFail(new Error(job.error || "生成失败")); return; }
        if (!host || !host.isConnected) return;   // 已切走：别往卸载的视图里写
        host.innerHTML = `<div class="card"><b>大纲生成失败</b><div class="hint">${esc(job.error || "")}</div>
          <div class="hint">多数是模型配置问题：没填 API、Key 无效、或端点连不上。</div>
          <div class="row" style="margin-top:12px">
            <button class="btn primary" id="retry">重新生成</button>
            <button class="btn" id="to-settings">去设置检查模型</button>
          </div></div>`;
        document.getElementById("retry").onclick = openCreate;
        const tsBtn = document.getElementById("to-settings");
        if (tsBtn) tsBtn.onclick = () => { location.hash = "#/settings"; };
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

  /** 讲次教学设计 desc → 折叠展示块（只读；无 desc 返回空串）。
   *
   * 视觉：每个字段一行「标签 chip + 内容」。原先 5 行等宽灰字挤在一起，
   * 关键约束（学习目标/知识点边界）和提示（可视化/术语口径）看不出区别 ——
   * 用户反馈「这个栏有点丑」。现在按语义给标签上色，内容用正文色、行距放开。
   */
  function descHtml(d) {
    if (!d || typeof d !== "object") return "";
    const rows = [];
    const push = (k, v, cls) => { if (v) rows.push({ k, v, cls: cls || "" }); };
    push("学习目标", d.outcomes && d.outcomes.join("；"), "goal");
    push("知识点边界", d.knowledge_points && d.knowledge_points.join("；"), "kp");
    push("术语口径", d.concepts && d.concepts.join("、"), "");
    push("涉及操作", d.operations && d.operations.join("；"), "");
    const tr = d.transition || {};
    const seg = [];
    if (tr.prev) seg.push(`承接 ${tr.prev}`);
    if (tr.next) seg.push(`引向 ${tr.next}`);
    if (tr.avoid) seg.push(`避免展开 ${tr.avoid}`);
    push("讲间衔接", seg.join("；"), "tr");
    push("可视化", d.visual && d.visual !== "无" ? d.visual : "", "vis");
    push("考察点", d.exercise_focus && d.exercise_focus.join("；"), "goal");
    push("易错点", d.expected_mistakes && d.expected_mistakes.join("；"), "tr");
    push("题型安排", d.exercise_flow, "");
    if (!rows.length) return "";
    // 内容统一在这里 esc（不要在拼串阶段转义，否则会双重转义）
    return `<details class="lesson-desc"><summary>教学设计 · AI 按此备课</summary>
      <div class="dd-body">${rows.map((r) =>
        `<div class="dd-row"><span class="dd-k ${r.cls}">${r.k}</span>`
        + `<span class="dd-v">${esc(r.v)}</span></div>`).join("")}</div></details>`;
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
    // 上次改到一半切页了 → 用本地那份改动，而不是服务端版本（用户实测：改动全丢）
    const saved = loadConfirmDraft();
    const restored = !!(saved && saved.courseId === courseId
      && Array.isArray(saved.units) && saved.units.length);
    if (restored) {
      draft.title = saved.title || draft.title;
      draft.units = saved.units;
    }
    // 当前单元数：决定「保持当前」选项的文案；不在 2~6 时默认选中「自定义…」
    const curUnits = draft.units.length;
    const card = el("div", "card");
    card.innerHTML = `
      <div class="row" style="justify-content:space-between">
        <b>${editing ? "编辑课程结构" : "确认课程结构"}</b>
        <span class="pill">${editing ? "修改后保存即生效" : "第 2 步 / 共 2 步"}</span>
      </div>
      <div class="hint">左边改标题与学习目标、增删讲次；右边是同一份结构的思维导图，改完立即同步。</div>
      ${restored ? `<div class="hint">↩ 已恢复你上次未保存的结构改动（保存于 ${draftTimeText(saved.at)}）。`
        + "点「保存结构」才会写回课程；否则下次进来自动带出这份改动。</div>" : ""}
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
      <div class="field" style="margin-top:12px">
        <label>单元数量（重新生成时）</label>
        <select id="o-units">
          <option value="">保持当前（${curUnits} 个单元）</option>
          <option value="0">自动（按材料定）</option>
          ${[2, 3, 4, 5, 6].map((n) => `<option value="${n}">${n} 个单元</option>`).join("")}
          <option value="custom">自定义…</option>
        </select>
        <input type="number" id="o-units-custom" min="1" max="12" placeholder="1 ~ 12"
               style="display:none;margin-top:6px;max-width:110px">
        <div class="hint">现在这门课是 ${curUnits} 个单元。改这里，或在上面的要求里写「生成 5 章」，点「重新生成」就会按新数量出纲。</div>
      </div>
      <div class="row" style="justify-content:flex-end">
        <button class="btn" id="o-regen">重新生成</button>
        <button class="btn primary" id="o-ok">${editing ? "保存结构" : "确认，开始学习"}</button>
      </div>`;
    host.appendChild(card);

    // 单元数量下拉：选「自定义…」展开数字框（1~12）；当前单元数不在 2~6 时默认选中自定义并预填 N
    const oUnitsSel = document.getElementById("o-units");
    const oUnitsCustom = document.getElementById("o-units-custom");
    oUnitsSel.onchange = () => {
      const custom = oUnitsSel.value === "custom";
      oUnitsCustom.style.display = custom ? "" : "none";
      if (custom) oUnitsCustom.focus();
    };
    // curUnits=0（结构为空）时不预选「自定义」，否则提交会被 clamp 成 1 个单元
    if (curUnits >= 1 && (curUnits < 2 || curUnits > 6)) {
      oUnitsSel.value = "custom";
      oUnitsCustom.value = curUnits;
      oUnitsCustom.style.display = "";
    }

    const tree = document.getElementById("tree");
    // 结构改动**本地保留**：切页、点左栏课程回来接着改（用户实测「改动全丢」）。
    // 保存成功后清掉，避免把已经生效的版本当成「未保存改动」反复带出来。
    confirmSaver = () => saveConfirmDraft({
      courseId: courseId, title: draft.title, units: draft.units,
    });
    scheduleConfirmSave();
    const paint = () => {
      tree.innerHTML = "";
        draft.units.forEach((u, ui) => {
          const box = el("div", "unit-box");
          const head = el("div", "row");
          head.innerHTML = `<input type="text" value="${esc(u.title)}" data-u="${ui}" style="flex:1">
            <span class="hint" data-upstage="${ui}"></span>
            <button class="btn small" data-upre="${ui}">预生成整章</button>`;
          box.appendChild(head);
          // 「预生成整章」：拿单元 id 调接口，后台串行补齐该单元缺失的讲义与练习（幂等）。
          const upBtn = head.querySelector(`[data-upre="${ui}"]`);
          if (upBtn) upBtn.onclick = async () => {
            const st = head.querySelector(`[data-upstage="${ui}"]`);
            const uid = ((c.units || [])[ui] || {}).id;
            if (!uid) return Toast("这个单元还没落库，先保存结构再预生成", true);
            upBtn.disabled = true; upBtn.textContent = "排队中…";
            try {
              const r = await Api.post(`/api/courses/units/${uid}/prefetch`, {});
              const q = r.queued || 0, sk = r.skipped || 0;
              if (st) st.textContent = q ? `已排队 ${q} 项（跳过 ${sk}）` : `都已就绪（跳过 ${sk}）`;
              Toast(q ? `已排队 ${q} 项，后台生成中` : "本章内容都已就绪");
            } catch (e) { Toast("预生成失败：" + e.message, true); }
            finally { upBtn.disabled = false; upBtn.textContent = "预生成整章"; }
          };
        u.lessons.forEach((l, li) => {
          const row = el("div", "lesson-row");
          row.innerHTML = `<span class="pill">${l.kind === "practice" ? "练习" : "讲解"}</span>`;
          const t = el("input", null); t.type = "text"; t.value = l.title; t.style.flex = "1";
          t.oninput = () => { l.title = t.value; drawMap(); scheduleConfirmSave(); };
          const o = el("input", null); o.type = "text"; o.value = l.objective; o.style.flex = "1";
          o.placeholder = "学习目标";
          o.oninput = () => { l.objective = o.value; drawMap(); scheduleConfirmSave(); };
          const x = el("button", "btn small", "删除");
          x.onclick = () => { u.lessons.splice(li, 1); paint(); scheduleConfirmSave(); };
          row.appendChild(t); row.appendChild(o); row.appendChild(x);
          box.appendChild(row);
          const dd = descHtml(l.desc);
          if (dd) box.insertAdjacentHTML("beforeend", dd);
        });
        const add = el("button", "btn small", "＋ 讲次");
        add.onclick = () => {
          u.lessons.push({ title: "新的讲次", objective: "", kind: "lecture" });
          paint();
          scheduleConfirmSave();
        };
        box.appendChild(add);
        box.querySelector(`[data-u="${ui}"]`).oninput = (e) => {
          u.title = e.target.value; drawMap(); scheduleConfirmSave();
        };
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
          clearConfirmDraft();      // 已写回服务端 → 这份「未保存改动」的使命结束
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
      // 单元数量：默认「保持当前」= 不带 unit_count（后端沿用当前）；
      // 「自动」=0；下拉数字 = 该数字；「自定义…」= 读数字框（1~12）。
      const oUnitsSel = document.getElementById("o-units");
      const oUnitsVal = oUnitsSel ? oUnitsSel.value : "";
      const body = { note };
      if (oUnitsVal !== "") {
        let unitCount;
        if (oUnitsVal === "custom") {
          let cv = parseInt((document.getElementById("o-units-custom") || {}).value || "", 10);
          if (!Number.isFinite(cv)) cv = curUnits;
          unitCount = Math.max(1, Math.min(12, cv));
        } else if (oUnitsVal === "0") {
          unitCount = 0;
        } else {
          unitCount = parseInt(oUnitsVal, 10);
        }
        body.unit_count = unitCount;
      }
      btn.disabled = true; btn.textContent = "重新生成中…";
      // 立刻给出反馈，避免「点了没反应」的错觉。
      showStage(host, note ? "正在按你的要求重新组织大纲…" : "正在重新生成大纲…");
      try {
        const r = await Api.post(`/api/courses/${courseId}/outline:regenerate`, body);
        await pollOutline(courseId, r.job_id, host);
      } catch (e) {
        Toast(e.message, true);
        btn.disabled = false; btn.textContent = "重新生成";
        renderMain();
      }
    };
  }

  /** 单元预生成的进度反馈：轮询该课程的 job 列表，直到没有 running。 */
  function pollUnitJobs(courseId, stageEl) {
    let n = 0;
    const tick = async () => {
      if (n++ > 240 || !stageEl || !stageEl.isConnected) return;
      let jobs = [];
      try {
        const r = await Api.get(`/api/courses/${courseId}/jobs`);
        jobs = (r && (r.items || r.jobs)) || (Array.isArray(r) ? r : []);
      } catch (e) { return; }
      const running = jobs.filter((j) => j.status === "running");
      if (running.length) {
        stageEl.textContent = `生成中：${running[0].stage || "处理中"}…`;
        setTimeout(tick, 2000);
      } else {
        stageEl.textContent = "预生成完成";
      }
    };
    setTimeout(tick, 800);
  }

  /* ── 课程详情 ─────────────────────────── */
  function renderDetail(host) {
    // 详情页只承载一份内容：先清空再画。否则「重新生成大纲」完成后的
    // 原地刷新（onReady → renderDetail）会把新详情 append 在旧详情下面，
    // 出现新旧大纲上下并排（用户实测问题 1）。
    host.innerHTML = "";
    const c = S.course;
    // 当前单元数：决定「保持当前」选项的文案；不在 2~6 时默认选中「自定义…」
    const curUnits = (c.units || []).length;
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
      ${p.total_lessons && (p.done_lessons || 0) >= p.total_lessons
        ? '<div class="course-done-banner">🏆 恭喜，这门课你已经学完了</div>' : ""}
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
      </div>
      <div class="field" style="margin-top:10px">
        <label>单元数量（重新生成时）</label>
        <select id="c-units">
          <option value="">保持当前（${curUnits} 个单元）</option>
          <option value="0">自动（按材料定）</option>
          ${[2, 3, 4, 5, 6].map((n) => `<option value="${n}">${n} 个单元</option>`).join("")}
          <option value="custom">自定义…</option>
        </select>
        <input type="number" id="c-units-custom" min="1" max="12" placeholder="1 ~ 12"
               style="display:none;margin-top:6px;max-width:110px">
        <div class="hint">现在这门课是 ${curUnits} 个单元。改这里，或在上面的要求里写「生成 5 章」，点「重新生成大纲」就会按新数量出纲。</div>
      </div>`;
    host.appendChild(head);
    // 单元数量下拉：选「自定义…」展开数字框（1~12）；当前单元数不在 2~6 时默认选中自定义并预填 N
    const cUnitsSel = document.getElementById("c-units");
    const cUnitsCustom = document.getElementById("c-units-custom");
    cUnitsSel.onchange = () => {
      const custom = cUnitsSel.value === "custom";
      cUnitsCustom.style.display = custom ? "" : "none";
      if (custom) cUnitsCustom.focus();
    };
    // curUnits=0（结构为空）时不预选「自定义」，否则提交会被 clamp 成 1 个单元
    if (curUnits >= 1 && (curUnits < 2 || curUnits > 6)) {
      cUnitsSel.value = "custom";
      cUnitsCustom.value = curUnits;
      cUnitsCustom.style.display = "";
    }
    document.getElementById("c-edit").onclick = () => {
      location.hash = "#/courses?confirm=" + c.id;
    };
    document.getElementById("c-regen").onclick = async () => {
      const stage = document.getElementById("c-stage");
      const btn = document.getElementById("c-regen");
      const noteEl = document.getElementById("c-regen-note");
      const note = (noteEl && noteEl.value || "").trim();
      // 单元数量：默认「保持当前」= 不带 unit_count；「自动」=0；下拉数字=该数字；「自定义…」=读数字框
      const cUnitsSel = document.getElementById("c-units");
      const cUnitsVal = cUnitsSel ? cUnitsSel.value : "";
      const body = { note };
      if (cUnitsVal !== "") {
        let unitCount;
        if (cUnitsVal === "custom") {
          let cv = parseInt((document.getElementById("c-units-custom") || {}).value || "", 10);
          if (!Number.isFinite(cv)) cv = curUnits;
          unitCount = Math.max(1, Math.min(12, cv));
        } else if (cUnitsVal === "0") {
          unitCount = 0;
        } else {
          unitCount = parseInt(cUnitsVal, 10);
        }
        body.unit_count = unitCount;
      }
      btn.disabled = true; btn.textContent = "重新生成中…";
      if (stage) stage.textContent = note ? "正在按你的要求重新组织大纲…" : "正在重新生成大纲…";
      const resetBtn = () => {
        const b = document.getElementById("c-regen");
        if (b) { b.disabled = false; b.textContent = "重新生成大纲"; }
        const st = document.getElementById("c-stage");
        if (st) st.textContent = "";
      };
      try {
        const r = await Api.post(`/api/courses/${c.id}/outline:regenerate`, body);
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
      box.innerHTML = `<div class="row" style="justify-content:space-between;align-items:center">
          <b>第 ${u.ordinal} 单元 · ${esc(u.title)}</b>
          <span class="row" style="gap:6px;align-items:center">
            <span class="hint" data-upstage="${u.id}"></span>
            <button class="btn small" data-upre="${u.id}">预生成整章</button>${tag}
          </span></div>
        ${u.summary ? `<div class="hint">${esc(u.summary)}</div>` : ""}
        ${st.questions
          ? `<div class="hint">练习：答对 ${st.correct}/${st.questions} · 得分 ${st.score}${
              st.errors ? ` · 错题 ${st.errors}` : ""}</div>`
          : ""}`;
      // 「预生成整章」：后台串行补齐该单元缺失的讲义与练习（幂等，已有内容会跳过）。
      const upBtn = box.querySelector(`[data-upre="${u.id}"]`);
      if (upBtn) upBtn.onclick = async () => {
        const st = box.querySelector(`[data-upstage="${u.id}"]`);
        upBtn.disabled = true; upBtn.textContent = "排队中…";
        try {
          const r = await Api.post(`/api/courses/units/${u.id}/prefetch`, {});
          const q = r.queued || 0, sk = r.skipped || 0;
          if (st) st.textContent = q ? `已排队 ${q} 项（跳过 ${sk}）` : `都已就绪（跳过 ${sk}）`;
          Toast(q ? `已排队 ${q} 项，后台生成中` : "本章内容都已就绪");
          if (q) pollUnitJobs(c.id, st);
        } catch (e) { Toast("预生成失败：" + e.message, true); }
        finally { upBtn.disabled = false; upBtn.textContent = "预生成整章"; }
      };
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

  /**
   * 大纲就绪后的收尾：**清草稿**（这次创建已经走完向导阶段）→ 刷新列表 → 进结构确认。
   * 与 pollOutline 的默认分支等价，区别只是顺带把草稿清掉，所以显式传 onReady。
   */
  async function afterOutlineReady(host, courseId) {
    clearDraft();
    await loadCourses();
    await loadMaterialJobs();     // 材料可能已被本课用掉 → 刷新「AI 材料」区块
    if (!host || !host.isConnected) return;   // 已切走：只刷数据，别碰 DOM
    await loadCourse(courseId);
    renderList();
    S.creating = false;
    confirmOutline(host, courseId);
  }

  /** 回到「正在生成课程大纲…」界面并**续上轮询**。
   *
   *  用户实测问题：点了「生成大纲」之后切到别的页，就再也回不到这个界面了
   *  （只能在课程列表里看到一条「生成中」的课程，点进去是空的详情页）。
   *  草稿里存了 course_id + job_id，所以这里能把那个界面原样重建并接着等。
   */
  async function resumeOutlineJob(host, job) {
    host.innerHTML = "";
    const card = el("div", "card");
    card.innerHTML = `<b>正在生成课程大纲…</b>
      <div class="hint" style="margin-top:8px" id="rs-stage">正在接回上次的任务…</div>
      <div class="bar"><i style="width:100%"></i></div>
      <div class="hint">时间取决于模型速度，通常十几秒到一分钟。可以切到别的页，回来自动继续。</div>
      <div class="row" style="margin-top:10px">
        <button class="btn small" id="rs-drop">不再等它，去看课程列表</button>
      </div>`;
    host.appendChild(card);
    const drop = document.getElementById("rs-drop");
    if (drop) {
      // 「不再等」只清掉这条草稿记录：课程本身已经建好了，不会因为这条丢东西
      drop.onclick = () => { clearDraft(); Toast("已收起这次创建，课程在左侧列表里", false); openCourse(job.courseId); };
    }
    const stage = document.getElementById("rs-stage");
    await pollOutline(job.courseId, job.jobId, host, stage,
      () => afterOutlineReady(host, job.courseId),
      () => dropDraftJob());
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
